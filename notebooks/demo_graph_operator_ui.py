# %% [markdown]
# # Graph Operator UI — Hands-On Tour
#
# A hands-on walkthrough that **interactively drives the contextualization
# pipeline** for one document — split → summarize → contextualize → persist,
# one step per cell — then inspects what was persisted, searches the content
# index, and finally spins up the **graph operator Web UI** so you can click
# through the jobs monitor and the content explorer over the very data you just
# built.
#
# **Run model:** open in VS Code Interactive or Jupyter and execute cells
# top-to-bottom with **Shift+Enter**. State lives in notebook-scope variables
# that later cells reuse, so run the cells in order (the way you would the
# `ai-vfs` tour). Re-running a cell is safe.
#
# **Zero infrastructure:** a deterministic `StubLLMClient` stands in for the
# model (no endpoint), and a fake `BlobReader` stands in for S3 — so the genuine
# code paths run with nothing external. Swap in a real model client at the
# summarize/contextualize cells to see live output instead of the demo stub.
#
# **Throwaway data:** everything goes into a temp SQLite DB; the real
# `data/conversion_service.db` is never touched. Run the final **Cleanup** cell
# (or restart the kernel) when you are done.

# %%
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from uuid import UUID, uuid4

from sqlmodel import Session, select
import uvicorn
import xxhash

from aizk.chunking import SPLITTER_VERSION, split
from aizk.graph.api.dependencies import get_blob_reader
from aizk.graph.api.main import create_app
from aizk.graph.contextualization import (
    VARIANT_STAGE,
    generate_revisions,
    generate_summary_text,
    resolve_chunk_text,
)
from aizk.graph.datamodel import ContextualizationJob, ContextualizedChunk
from aizk.graph.enqueue import enqueue_output
from aizk.graph.handler import ContextualizationStageHandler
from aizk.graph.markdown_source import ConversionOutputFreshness
from aizk.graph.persistence import active_chunking_run, document_order_chunks
from aizk.graph.search import Fts5SearchProvider, SearchKind
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.run import PipelineRun, RunStatus
from aizk.pipeline.runner import StageRunner

logging.basicConfig(level=logging.WARNING)

# The graph app's TrustedHostMiddleware admits these; a browser on localhost uses one.
GRAPH_UI_HOST = "127.0.0.1"
GRAPH_UI_PORT = 8800

# A term the demo stub injects ONLY into contextualized revisions — search it under
# the "contextualized" type filter to see a hit that exists on no raw chunk.
REVISION_SENTINEL = "situated"

# %% [markdown]
# ## 1. Setup (boilerplate)
#
# `demo_setup` is kept a function because it is boilerplate you rarely step
# through: it points `AIZK_DATABASE_URL` at a fresh temp SQLite file (and allows
# the loopback Host) **before** any config/engine is built, runs the Alembic
# migrations (which create the `graph_content_fts` index this tour searches), and
# returns the engine plus the deterministic stub model client.
#
# The deterministic stub: on a **summary** prompt it returns a one-line summary
# grounded in the document's title; on a **contextualization** prompt it returns
# either a self-contained revision (prefixed with a clause carrying the
# `situated` sentinel) or — for ~1 chunk in 3, by a stable hash — an empty
# revision meaning "already self-contained". Because it is deterministic, the
# preview cells below show exactly what the worker will persist.


# %%
def _demo_responder(prompt: str) -> str:
    """Return a deterministic summary or per-chunk revision for a built prompt.

    Branches on the prompt-kind marker the contextualization prompts begin with
    and reads the embedded JSON payload so the output is grounded in the actual
    document/chunk text rather than random.
    """
    from aizk.graph.contextualization import CONTEXT_PROMPT_KIND, SUMMARY_PROMPT_KIND

    parts = prompt.split("\n", 2)
    kind = parts[0]
    try:
        payload = json.loads(parts[2]) if len(parts) > 2 else {}
    except (ValueError, IndexError):
        payload = {}

    if kind == SUMMARY_PROMPT_KIND:
        document = str(payload.get("document", "")).strip()
        first_line = document.splitlines()[0].lstrip("# ").strip() if document else "this document"
        return (
            f"Demo summary of '{first_line}': a seeded source for exploring the graph operator UI. "
            "It defines the key terms used across its sections so individual chunks can be situated."
        )
    if kind == CONTEXT_PROMPT_KIND:
        working = str(payload.get("working_chunk", "")).strip()
        if not working:
            return ""
        if xxhash.xxh64(working.encode("utf-8")).intdigest() % 3 == 0:
            return ""  # the stub judges this chunk already self-contained
        return f"Demo contextualized revision, {REVISION_SENTINEL} within its document: {working}"
    return ""


def demo_setup() -> tuple:
    """Isolate a temp DB, run migrations, and return ``(engine, stub_client, tmp_dir)``."""
    from aizk.graph.llm import StubLLMClient

    tmp_dir = tempfile.mkdtemp(prefix="aizk_graph_ui_tour_")
    tmp_url = f"sqlite:///{Path(tmp_dir) / 'graph_ui_tour.db'}"
    os.environ["AIZK_DATABASE_URL"] = tmp_url
    os.environ["AIZK_TRUSTED_HOSTS"] = json.dumps([GRAPH_UI_HOST, "localhost"])

    from aizk.conversion.db import get_engine
    from aizk.conversion.migrations import run_migrations
    from aizk.conversion.utilities.config import ConversionConfig

    active_url = ConversionConfig().database_url
    if active_url != tmp_url or active_url.endswith("conversion_service.db"):
        raise RuntimeError(f"Temp-DB isolation failed: active URL is {active_url!r}, expected {tmp_url!r}.")

    run_migrations()
    engine = get_engine(active_url)
    print(f"temp database : {tmp_url}")
    print("migrations    : applied (graph_content_fts index present)")
    return engine, StubLLMClient(responder=_demo_responder), tmp_dir


# %%
engine, client, tmp_dir = demo_setup()

# %% [markdown]
# ## 2. The focal document and its conversion output
#
# The graph stage consumes a **conversion output** (the converted Markdown plus a
# locator). We seed one `Source` + `ConversionJob` + `ConversionOutput` for our
# focal document (the FK chain the real engine enforces) and register its Markdown
# in two in-memory doubles: a `LocalMarkdownSource` the worker reads to split, and
# a `FakeBlobReader` the explorer's detail panel reads to reconstruct the source
# Markdown — so nothing needs S3.


# %%
class FakeBlobReader:
    """In-memory ``BlobReader`` mapping a conversion output's ``markdown_key`` to bytes."""

    def __init__(self) -> None:
        """Initialize an empty key-to-bytes registry."""
        self._by_key: dict[str, bytes] = {}

    def register(self, markdown_key: str, text: str) -> None:
        """Register a Markdown key's UTF-8 bytes."""
        self._by_key[markdown_key] = text.encode("utf-8")

    def get_object_bytes(self, s3_key: str) -> bytes:
        """Return the registered bytes for ``s3_key``."""
        return self._by_key[s3_key]


class LocalMarkdownSource:
    """In-memory ``MarkdownSource`` mapping a ``conversion_output_id`` to its Markdown."""

    def __init__(self) -> None:
        """Initialize an empty output-id-to-Markdown registry."""
        self._by_output: dict[int, object] = {}

    def register(self, conversion_output_id: int, text: str, markdown_hash: str) -> None:
        """Register a conversion output's Markdown text + recorded hash."""
        from aizk.graph.workunit import LoadedMarkdown

        self._by_output[conversion_output_id] = LoadedMarkdown(text=text, markdown_hash_xx64=markdown_hash)

    def load(self, conversion_output_id: int):
        """Return the registered Markdown for a conversion output locator."""
        return self._by_output[conversion_output_id]


def seed_conversion_output(engine, *, text: str, label: str, markdown_source, blob_reader) -> tuple[UUID, int, str]:
    """Seed a Source + ConversionJob + ConversionOutput and register its Markdown.

    Returns ``(aizk_uuid, conversion_output_id, markdown_hash)``.
    """
    from sqlmodel import Session

    from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
    from aizk.conversion.datamodel.output import ConversionOutput
    from aizk.conversion.datamodel.source import Source
    from aizk.utilities.hashing import compute_markdown_hash

    aizk_uuid = uuid4()
    markdown_hash = compute_markdown_hash(text)
    markdown_key = f"demo/{aizk_uuid}/output.md"
    ref = f"demo://{aizk_uuid}"
    with Session(engine) as session:
        session.add(
            Source(
                aizk_uuid=aizk_uuid,
                source_ref=ref,
                source_ref_hash=compute_markdown_hash(ref),
                owner_id="demo",
                title=label,
            )
        )
        session.flush()
        job = ConversionJob(
            aizk_uuid=aizk_uuid,
            owner_id="demo",
            title=label,
            idempotency_key=f"demo-conv-{uuid4().hex}",
            status=ConversionJobStatus.SUCCEEDED,
        )
        session.add(job)
        session.flush()
        output = ConversionOutput(
            job_id=job.id,
            aizk_uuid=aizk_uuid,
            owner_id="demo",
            title=label,
            payload_version=1,
            s3_prefix=f"demo/{aizk_uuid}",
            markdown_key=markdown_key,
            manifest_key=f"demo/{aizk_uuid}/manifest.json",
            markdown_hash_xx64=markdown_hash,
            docling_version="demo",
            pipeline_name="docling",
        )
        session.add(output)
        session.flush()
        output_id = output.id
        session.commit()
    markdown_source.register(output_id, text, markdown_hash)
    blob_reader.register(markdown_key, text)
    return aizk_uuid, output_id, markdown_hash


# %%
FOCAL_MARKDOWN = """# The Transformer Architecture

The Transformer is a sequence-to-sequence model introduced by Vaswani et al.
It dispenses with recurrence entirely and relies only on attention.

## Self-Attention

It computes, for each position, a weighted sum of value vectors whose weights
come from a compatibility score. This mechanism is the core of the approach
described above.

## Results

The model reaches state-of-the-art quality on machine translation while training
faster. These gains build directly on the architecture introduced earlier.
"""

markdown_source = LocalMarkdownSource()
blob_reader = FakeBlobReader()
focal_uuid, focal_output_id, focal_hash = seed_conversion_output(
    engine,
    text=FOCAL_MARKDOWN,
    label="transformer-architecture",
    markdown_source=markdown_source,
    blob_reader=blob_reader,
)
print(f"aizk_uuid            : {focal_uuid}")
print(f"conversion_output_id : {focal_output_id}")
print(f"markdown_hash        : {focal_hash}")

# %% [markdown]
# ## 3. Split the document into chunks
#
# `split()` is the pure, deterministic structural splitter — identical input
# always yields identical chunks in document order. Each chunk carries a
# content-addressed `chunk_id`, its `heading_path`, an `ordinal` within that
# heading, and (from the splitter) its character `span` in the source Markdown.
# No database is touched yet. Edit `FOCAL_MARKDOWN` above and re-run to watch the
# chunk set change.

# %%
chunks = split(
    FOCAL_MARKDOWN,
    doc_id=str(focal_uuid),
    converted_artifact_id=str(focal_output_id),
    markdown_hash_xx64=focal_hash,
)
print(f"splitter v{SPLITTER_VERSION} produced {len(chunks)} chunks (document order):\n")
for chunk in chunks:
    preview = chunk.text.strip().replace("\n", " ")[:64]
    print(f"  {chunk.chunk_id[:12]}  span={chunk.span}  heading={chunk.heading_path}  ord={chunk.ordinal}")
    print(f"      {preview!r}")

# %% [markdown]
# ## 4. Summarize the document
#
# Contextualization runs one **summary** pass over the whole document; the
# per-chunk revisions are conditioned on this summary. `generate_summary_text` is
# pure model I/O — it builds the summary prompt and calls `client.generate`, with
# no database access. Here the stub answers; swap `client` for a real
# `PydanticAILLMClient` to see a genuine summary.

# %%
summary_text = generate_summary_text(client, FOCAL_MARKDOWN)
print("document summary:\n")
print(f"  {summary_text}")

# %% [markdown]
# ## 5. Contextualize each chunk
#
# For each chunk the model rewrites it into a **self-contained** passage,
# resolving references (pronouns, "the approach described above") inline using a
# 2-prior / 1-next window plus the summary. An **empty** revision means the model
# judged the chunk already self-contained — downstream that consumes the raw
# chunk unchanged, and the explorer marks it "self-contained".
#
# `generate_revisions` is pure model I/O (no persistence). Because the stub is
# deterministic, these are exactly the revisions the worker will persist in the
# next cell. Note the `situated` sentinel the stub injects into non-empty
# revisions — that word appears on no raw chunk, so it later demonstrates the
# "contextualized"-only search filter.

# %%
revisions = generate_revisions(client, summary_text, chunks)
for chunk, revision in zip(chunks, revisions, strict=True):
    print(f"--- chunk {chunk.chunk_id[:12]} ---")
    print(f"  raw     : {chunk.text.strip().replace(chr(10), ' ')[:80]!r}")
    shown = revision.strip().replace("\n", " ")[:90] if revision.strip() else "<empty: already self-contained>"
    print(f"  revision: {shown!r}\n")

# %% [markdown]
# ## 6. Run the work-unit through the real worker
#
# The previews above are exactly what the runtime persists. Now drive the genuine
# `ContextualizationStageHandler` under a `StageRunner` — the same path
# `aizk-graph worker` runs. Enqueuing the conversion output and draining the
# runner performs `split → persist_chunks → summarize → contextualize` inside one
# short transaction, which:
#
# * records the chunking / document-summary / chunk-contextualization **runs**,
# * persists the chunk rows, the summary, and the per-chunk variants,
# * inserts the rows into the `graph_content_fts` **search index**, and
# * drives the work-unit lifecycle, leaving a `claimed → succeeded` **event
#   trail** the operator jobs drill-down reads.
#
# (The stub makes the worker re-derive the same summary/revisions you previewed,
# so what you saw is what lands.)

# %%
handler = ContextualizationStageHandler(engine, client, markdown_source, ConversionOutputFreshness())
with Session(engine) as session:
    enqueue_output(session, focal_output_id)
    session.commit()
StageRunner(
    engine=engine, handler=handler, poll_interval=0.05, stale_recovery_interval=3600.0, cancel_grace=2.0
).run_until_idle(max_iterations=10_000)

with Session(engine) as session:
    focal_job = session.exec(
        select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == focal_output_id)
    ).one()
    events = session.exec(
        select(PipelineEvent)
        .where(PipelineEvent.stage == "contextualization", PipelineEvent.work_unit_ref == str(focal_job.id))
        .order_by(PipelineEvent.event_id)
    ).all()
print(f"work-unit id={focal_job.id} status={focal_job.status.value} attempts={focal_job.attempts}")
for event in events:
    print(f"  event {event.from_status} -> {event.to_status}  kind={event.kind}")

# %% [markdown]
# ## 7. Inspect what was persisted
#
# Read it back through the same helpers the explorer uses: the active chunking
# run's chunks in **document (`span_start`) order**, each paired with its current
# contextualized representation via `resolve_chunk_text` (the stored revision, or
# the raw chunk when the revision is empty — tagged so a consumer knows which it
# got). This is precisely what the explorer's spine + detail panel render.

# %%
with Session(engine) as session:
    run = active_chunking_run(session, str(focal_uuid))
    variant_run = session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == VARIANT_STAGE,
            PipelineRun.scope_key == str(focal_uuid),
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one()
    variants = {
        v.chunk_id: v.contextualized_text
        for v in session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)).all()
    }
    print(f"active chunking run id={run.id}; active variant run id={variant_run.id}\n")
    for chunk_row, span_start, _span_end in document_order_chunks(session, run.id):
        resolved = resolve_chunk_text(
            chunk_row.text,
            contextualized_text=variants.get(chunk_row.chunk_id),
            contextualization_enabled=True,
        )
        self_contained = variants.get(chunk_row.chunk_id) == ""
        marker = "  [self-contained]" if self_contained else ""
        print(f"  span={span_start:<4} {json.loads(chunk_row.heading_path_json)}{marker}")
        print(f"      current ({resolved.source.value}): {resolved.text.strip().replace(chr(10), ' ')[:84]!r}\n")

# %% [markdown]
# ## 8. Search the content index
#
# The `Fts5SearchProvider` runs the ranked, type-filtered query the explorer's
# search box uses: it matches the FTS index, filters to active-generation content,
# aggregates per chunk (one result carrying per-side match flags), and ranks by
# `bm25` then document order. Operator input is treated as **literal terms**, so
# query-syntax characters never error.
#
# Try the cells: a raw term (`attention`) matches the chunk side; the `situated`
# sentinel matches only the **contextualized** side; an empty query returns
# nothing (not the whole corpus).

# %%
provider = Fts5SearchProvider(engine)


def show_results(query: str, kind: SearchKind) -> None:
    """Run one search and print each result's per-side match flags + score."""
    results = provider.search(query, kind)
    print(f"search {query!r} [{kind.value}] -> {len(results)} result(s)")
    for r in results:
        sides = []
        if r.matched_in_chunk:
            sides.append("raw")
        if r.matched_in_contextualized:
            sides.append("contextualized")
        print(f"  chunk={r.chunk_id[:12]} span={r.span_start} bm25={r.score:.3f} matched={'+'.join(sides)}")


show_results("attention", SearchKind.EITHER)
print()
show_results(REVISION_SENTINEL, SearchKind.CONTEXTUALIZED)
print()
show_results(REVISION_SENTINEL, SearchKind.CHUNK)  # the sentinel is on no raw chunk -> 0 results
print()
show_results("", SearchKind.EITHER)  # empty -> empty, never the corpus

# %% [markdown]
# ## 9. Populate the rest of the corpus (boilerplate)
#
# To make the jobs monitor and cross-document search interesting, seed the
# remaining surfaces in bulk (kept as functions — this is context, not the focus):
#
# * two more fully-contextualized documents (distinct vocabularies, for ranking),
# * a few work-units parked in failed / queued / cancelled states (for the status
#   filter and bulk Retry/Cancel), and
# * one source that was **chunked but never contextualized** — so the jobs
#   drill-down shows the contextualization run *absent* with a failure event, and
#   the explorer shows its raw chunks with *no* contextualized representation.


# %%
_MORE_DOCUMENTS = {
    "deep-sea-bioluminescence": """# Deep-Sea Bioluminescence

Many deep-sea organisms emit light through a chemical reaction between luciferin
and the enzyme luciferase. It serves camouflage, predation, and communication.

## The Reaction

The reaction oxidizes luciferin, releasing energy as a photon of visible light.
This process is remarkably efficient, wasting little energy as heat.

## Counter-Illumination

Some squid use the reaction described above to match downwelling light, erasing
their silhouette from predators looking up from below.
""",
    "sourdough-fermentation": """# Sourdough Fermentation

A sourdough starter is a stable culture of wild yeast and lactic acid bacteria.
It leavens bread and develops its characteristic sour flavor.

## The Levain

Before mixing the dough, a baker refreshes a portion of the starter into a
levain. This build wakes the culture so it ferments the dough vigorously.

## Flavor

The bacteria produce lactic and acetic acids during a long, cool fermentation.
These acids give the crumb the tang that defines the style.
""",
}


def contextualize_more_documents() -> list[UUID]:
    """Seed + contextualize the remaining documents through the real worker."""
    ids: list[UUID] = []
    for label, text in _MORE_DOCUMENTS.items():
        aizk_uuid, output_id, _ = seed_conversion_output(
            engine, text=text, label=label, markdown_source=markdown_source, blob_reader=blob_reader
        )
        with Session(engine) as session:
            enqueue_output(session, output_id)
            session.commit()
        ids.append(aizk_uuid)
    StageRunner(
        engine=engine, handler=handler, poll_interval=0.05, stale_recovery_interval=3600.0, cancel_grace=2.0
    ).run_until_idle(max_iterations=10_000)
    return ids


def seed_assorted_job_states() -> None:
    """Seed a failed, a queued, and a cancelled work-unit on fresh demo sources."""
    from aizk.conversion.datamodel.source import Source
    from aizk.pipeline.lifecycle import WorkUnitStatus
    from aizk.utilities.hashing import compute_markdown_hash

    specs = [
        ("retryable failed note", WorkUnitStatus.FAILED, 3, "demo_timeout"),
        ("queued note", WorkUnitStatus.QUEUED, 0, None),
        ("cancelled note", WorkUnitStatus.CANCELLED, 1, None),
    ]
    with Session(engine) as session:
        for offset, (label, status, attempts, error_code) in enumerate(specs):
            aizk_uuid = uuid4()
            ref = f"demo://{aizk_uuid}"
            session.add(
                Source(
                    aizk_uuid=aizk_uuid,
                    source_ref=ref,
                    source_ref_hash=compute_markdown_hash(ref),
                    owner_id="demo",
                    title=label,
                )
            )
            session.add(
                ContextualizationJob(
                    aizk_uuid=aizk_uuid,
                    conversion_output_id=800_000 + offset,
                    idempotency_key=f"demo-state-{uuid4().hex}",
                    status=status,
                    attempts=attempts,
                    error_code=error_code,
                )
            )
        session.commit()


def seed_chunked_not_contextualized() -> UUID:
    """Chunk a source without contextualizing it; fail its work-unit after chunking."""
    from aizk.graph.db import begin_immediate
    from aizk.graph.events import CONTEXTUALIZATION_STAGE, ClaimedPayload, FailedPayload, GraphEventKind
    from aizk.graph.persistence import persist_chunks
    from aizk.pipeline.events import record_transition
    from aizk.pipeline.lifecycle import WorkUnitStatus

    text = (
        "# Partially Processed Note\n\n"
        "This note was chunked but its contextualization did not complete.\n\n"
        "## Detail\n\n"
        "The raw chunks are searchable and visible in the explorer spine, but the "
        "source has no active variant run, so no contextualized representation exists.\n"
    )
    aizk_uuid, output_id, markdown_hash = seed_conversion_output(
        engine, text=text, label="chunked-not-contextualized", markdown_source=markdown_source, blob_reader=blob_reader
    )
    gap_chunks = split(
        text, doc_id=str(aizk_uuid), converted_artifact_id=str(output_id), markdown_hash_xx64=markdown_hash
    )
    now = dt.datetime.now(dt.timezone.utc)
    with begin_immediate(engine) as session:
        persist_chunks(
            session,
            aizk_uuid=str(aizk_uuid),
            conversion_output_id=str(output_id),
            markdown_hash_xx64=markdown_hash,
            splitter_version=SPLITTER_VERSION,
            chunks=gap_chunks,
        )
        job = ContextualizationJob(
            aizk_uuid=aizk_uuid,
            conversion_output_id=output_id,
            idempotency_key=f"demo-gap-{uuid4().hex}",
            status=WorkUnitStatus.QUEUED,
            attempts=1,
        )
        session.add(job)
        session.flush()
        record_transition(
            session,
            job,
            stage=CONTEXTUALIZATION_STAGE,
            work_unit_ref=str(job.id),
            aizk_uuid=aizk_uuid,
            to_status=WorkUnitStatus.RUNNING,
            kind=GraphEventKind.CLAIMED,
            attempt=1,
            payload=ClaimedPayload(claimed_at=now),
            from_status=WorkUnitStatus.QUEUED,
        )
        record_transition(
            session,
            job,
            stage=CONTEXTUALIZATION_STAGE,
            work_unit_ref=str(job.id),
            aizk_uuid=aizk_uuid,
            to_status=WorkUnitStatus.FAILED,
            kind=GraphEventKind.FAILED,
            attempt=1,
            payload=FailedPayload(
                error_code="demo_contextualization_error",
                error_message="Contextualization failed after chunking (seeded demo failure).",
                retryable=True,
            ),
        )
    return aizk_uuid


# %%
more_uuids = contextualize_more_documents()
seed_assorted_job_states()
gap_uuid = seed_chunked_not_contextualized()
print(
    f"contextualized {len(more_uuids)} more documents; seeded assorted job states + 1 chunked-not-contextualized source"
)

# %% [markdown]
# ## 10. Serve the graph operator Web UI
#
# Build the graph operator app (the one `aizk-graph` serves), inject the fake
# `BlobReader` so the detail panel reconstructs Markdown without S3, and run
# uvicorn in a background thread so this cell returns and the kernel keeps
# serving. The single app hosts **both** operator surfaces — the jobs monitor and
# the content explorer — behind the same trusted-host + principal perimeter as the
# JSON API.

# %%
app = create_app()
app.dependency_overrides[get_blob_reader] = lambda: blob_reader

_uvicorn_config = uvicorn.Config(app, host=GRAPH_UI_HOST, port=GRAPH_UI_PORT, log_level="warning")
server = uvicorn.Server(_uvicorn_config)
threading.Thread(target=server.run, name="graph-ui", daemon=True).start()
_deadline = time.monotonic() + 10.0
while not server.started and time.monotonic() < _deadline:
    time.sleep(0.05)

_base = f"http://{GRAPH_UI_HOST}:{GRAPH_UI_PORT}"
print("graph operator UI is serving — open these in your browser:\n")
print(f"  Jobs monitor : {_base}/ui/graph/jobs")
print("     filter by status, search, select rows -> bulk Retry/Cancel, open a job -> stage drill-down")
print(f"  Explorer     : {_base}/ui/graph/explorer")
print(f"     search 'attention' / 'luciferin' / 'levain'; '{REVISION_SENTINEL}' under the contextualized filter")
print("\n  Open a document browser directly:")
print(f"     transformer (focal) : {_base}/ui/graph/explorer?doc_id={focal_uuid}")
for uid in more_uuids:
    print(f"     more document       : {_base}/ui/graph/explorer?doc_id={uid}")
print(
    f"     chunked-not-contextualized (raw chunks, no contextualized rep): {_base}/ui/graph/explorer?doc_id={gap_uuid}"
)
print("\n  Stop: run the Cleanup cell below, or restart the kernel.")
print("  (Running this file as a plain script exits after starting the daemon server — run it interactively.)")

# %% [markdown]
# ## Where this lives
#
# The split → summarize → contextualize → persist unit of work is
# `aizk.graph.handler.ContextualizationStageHandler` under
# `aizk.pipeline.runner.StageRunner`; the pure model and read-time pieces are in
# `aizk.graph.contextualization` (`generate_summary_text`, `generate_revisions`,
# `resolve_chunk_text`) and the splitter in `aizk.chunking`. Persistence and the
# explorer's read helpers are in `aizk.graph.persistence`; the ranked, type-filtered
# search is `aizk.graph.search.Fts5SearchProvider`. The operator surfaces — the JSON
# API, the jobs monitor, and the content explorer — are in `aizk.graph.api`. Worked
# examples live in `tests/graph/`.

# %% [markdown]
# ## Cleanup
#
# Run this when you are done exploring: it stops the server, disposes the engine,
# and removes the temp directory.

# %%
server.should_exit = True
time.sleep(0.3)
engine.dispose()
shutil.rmtree(tmp_dir, ignore_errors=True)
print(f"stopped server and cleaned up {tmp_dir}")
