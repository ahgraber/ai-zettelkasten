# %% [markdown]
# # Graph Contextualization Worker — Hands-On Tour
#
# The graph stage turns converted Markdown into chunks a retriever can use
# *out of context*. A raw chunk lifted from the middle of a document is full of
# danglers — "this mechanism", "the approach described above", "these gains" —
# that only resolve against neighbours the retriever never returns. This worker
# rewrites each chunk into a **self-contained revision** with those references
# resolved inline, while keeping the original chunk byte-for-byte immutable, so a
# consumer can choose raw or revised text at read time.
#
# Read top to bottom, this notebook walks one document end to end — split →
# summarize → contextualize → persist — then proves the guarantees the design
# rests on. A few terms used throughout:
#
# - **conversion output** — one converted Markdown artifact (text + locator + hash)
#   for a source; the graph stage's input.
# - **chunk identity** — a content-addressed `chunk_id` carrying only stable facts
#   (text, heading path, ordinal); it never changes when a run re-derives it.
# - **chunking run / manifest / input** — a run records *which* conversion output it
#   consumed (`input`, with hash) and *where* each chunk fell in the source
#   (`manifest`, the character `span`). Run-scoped, not chunk-scoped.
# - **document summary** — one run-scoped summary per document; the per-chunk
#   revisions are conditioned on it.
# - **contextualized variant** — a chunk's self-contained revision; an *empty*
#   revision means the model judged the chunk already self-contained.
# - **resolve-at-use toggle** — `resolve_chunk_text` picks raw vs revised text at
#   read time; the raw chunk is never mutated.
# - **work-unit** — the queued unit of work the runner claims, executes, and
#   drives through a lifecycle; deduped on an idempotency key.
# - **generation / supersession** — re-converting a source produces a newer
#   generation and supersedes the prior one's runs; a **freshness gate** keeps
#   "current" monotonic so a late older output cannot drag it back.
#
# Step through in **VS Code Interactive** or **Jupyter**, top to bottom. This tour
# does **real model work**: the summary and per-chunk passes call a configured
# OpenAI-compatible endpoint, so you see genuine reference resolution — set
# `AIZK_GRAPH__CONTEXTUALIZATION__LLM_BASE_URL` / `_API_KEY` / `_MODEL` (the probe
# cell checks them). Everything writes to a throwaway temp SQLite database; the
# real `data/conversion_service.db` is never touched. Run the final **Cleanup**
# cell when you are done.

# %%
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import tempfile
from uuid import UUID, uuid4

from dotenv import load_dotenv
import nest_asyncio
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.utilities.config import ConversionConfig
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.db.migrations import run_migrations
from aizk.graph.api.main import create_app
from aizk.graph.config import ContextualizationConfig
from aizk.graph.contextualization import (
    SUMMARY_STAGE,
    VARIANT_STAGE,
    ContextSource,
    resolve_chunk_text,
)
from aizk.graph.datamodel import (
    Chunk,
    ContextualizationJob,
    ContextualizedChunk,
    DocumentSummary,
)
from aizk.graph.enqueue import enqueue_backfill_outputs, enqueue_output
from aizk.graph.handler import ContextualizationStageHandler
from aizk.graph.markdown_source import ConversionOutputFreshness
from aizk.graph.persistence import active_chunking_run, chunks_of_run, manifest_of_run, run_input
from aizk.graph.worker import build_llm_client
from aizk.graph.workunit import LoadedMarkdown
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, RunStatus
from aizk.pipeline.runner import StageRunner
from aizk.utilities.hashing import compute_markdown_hash

# %% [markdown]
# ## Load credentials and isolate a throwaway database (run first)
#
# `AIZK_DATABASE_URL` has to be set *before* any `ConversionConfig` is constructed,
# because every config reads the URL at construction time and `get_engine` caches
# engines by URL. `isolate_temp_database` points it at a fresh temp SQLite file and
# then guards that a freshly built config really resolved there, so a stray
# exported URL cannot send the demo at the real service database. `load_dotenv`
# brings in the model credentials, and `nest_asyncio` lets the model client's
# `run_sync` reuse the kernel's already-running event loop.

# %%
DEFAULT_DB_URL = "sqlite:///./data/conversion_service.db"


def isolate_temp_database() -> tuple[Path, str]:
    """Create a throwaway temp SQLite DB and point ``AIZK_DATABASE_URL`` at it.

    Sets the env var **before** any conversion config/engine is built, then
    asserts a freshly constructed :class:`ConversionConfig` resolves to the temp
    file (not the default service DB), so a stray exported URL cannot send the
    demo at the real database.

    Returns:
        The temp DB :class:`~pathlib.Path` and the ``sqlite:///`` URL it set.
    """
    tmp_dir = tempfile.mkdtemp(prefix="aizk_graph_demo_")
    tmp_db = Path(tmp_dir) / "graph_demo.db"
    tmp_url = f"sqlite:///{tmp_db}"
    os.environ["AIZK_DATABASE_URL"] = tmp_url

    active_url = DatabaseConfig().database_url
    if active_url != tmp_url:
        raise RuntimeError(
            f"Temp-DB isolation failed: DatabaseConfig().database_url is {active_url!r}, "
            f"expected {tmp_url!r}. Refusing to run against a non-temp database."
        )
    if active_url == DEFAULT_DB_URL or active_url.endswith("conversion_service.db"):
        raise RuntimeError(f"Active database URL {active_url!r} points at the default service DB. Refusing to run.")
    return tmp_db, tmp_url


# %%
# override=True so the .env's interpolated values win over any literal "${...}" the
# kernel may already hold (VS Code / direnv often load .env without interpolation).
_ = load_dotenv(override=True)
nest_asyncio.apply()
logging.basicConfig(level=logging.INFO)
logging.getLogger("aizk").setLevel(logging.INFO)

tmp_db, tmp_url = isolate_temp_database()
print(f"isolated temp database: {tmp_url}")

# %% [markdown]
# ## Probe the contextualization model before doing real work
#
# The whole point of this tour is watching a real model resolve references, so the
# summary and per-chunk passes call the configured OpenAI-compatible endpoint. This
# cell echoes the resolved settings (key masked) and issues one trivial call so a
# misconfigured endpoint fails *here* with guidance — not as opaque per-document
# work-unit failures later. If it is unreachable it prints which env vars to set
# (no traceback) and leaves `client` unset; the later cells need a live endpoint.

# %%
client = None
_ctx_config = ContextualizationConfig()
_key = _ctx_config.llm_api_key
print(
    f"contextualization model: base_url={_ctx_config.llm_base_url or 'MISSING'!r} "
    f"model={_ctx_config.llm_model or 'MISSING'!r} "
    f"api_key={'set (len=' + str(len(_key)) + ')' if _key else 'MISSING'}"
)
if "${" in (_ctx_config.llm_base_url + _ctx_config.llm_api_key + _ctx_config.llm_model):
    print("A model setting still contains a literal '${...}' — the .env interpolation did not reach this kernel.")
    print("Define the referent vars ABOVE these lines in .env, or set literal values, then restart the kernel.")
else:
    try:
        _candidate = build_llm_client(_ctx_config)  # raises StartupValidationError if the LLM_* triple is unset
        _reply = _candidate.generate("Reply with the single word: OK")
    except Exception as exc:  # noqa: BLE001 - probe degrades to guidance, no traceback
        print("Contextualization endpoint not usable — the cells below need it.")
        print("Set these (shell or .env), then restart the kernel:")
        print("  AIZK_GRAPH__CONTEXTUALIZATION__LLM_BASE_URL=<openai-compatible endpoint>")
        print("  AIZK_GRAPH__CONTEXTUALIZATION__LLM_API_KEY=<key>")
        print("  AIZK_GRAPH__CONTEXTUALIZATION__LLM_MODEL=<model id>")
        print(f"  reason: {type(exc).__name__}: {exc}")
    else:
        client = _candidate
        print(f"preflight OK — model replied {_reply.strip()[:80]!r}; later cells will call it.")

# %% [markdown]
# ## Load sample documents
#
# The worker processes converted documents, so prefer a prior Docling run's
# `output.md` files when present (smallest first, to keep the real model passes
# cheap) and otherwise fall back to one small vendored sample whose cross-references
# a real model resolves inline. `SAMPLE_SIZE` bounds how many documents run — each
# drives a summary call plus one revision call per chunk, so keep it small.

# %%
SAMPLE_SIZE = 2
DOCLING_OUTPUTS_DIR = Path("data/validate_docling_worker")

VENDORED_SAMPLE = """# The Transformer Architecture

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


@dataclass(frozen=True)
class DemoDoc:
    """One source document for the demo: a human label and its Markdown text."""

    label: str
    markdown_text: str


def load_demo_documents(sample_size: int) -> list[DemoDoc]:
    """Return up to ``sample_size`` documents, preferring prior Docling outputs.

    Reads ``data/validate_docling_worker/*/output.md`` (smallest first, so the
    real model passes stay cheap); falls back to the single vendored sample when
    none are present.
    """
    outputs = sorted(DOCLING_OUTPUTS_DIR.glob("*/output.md"), key=lambda p: p.stat().st_size)
    docs = [DemoDoc(label=path.parent.name, markdown_text=path.read_text(encoding="utf-8")) for path in outputs]
    if not docs:
        docs = [DemoDoc(label="vendored-transformer-sample", markdown_text=VENDORED_SAMPLE)]
    return docs[:sample_size]


# %%
documents = load_demo_documents(SAMPLE_SIZE)
print(f"loaded {len(documents)} document(s): {[d.label for d in documents]}")

# %% [markdown]
# ## Seed the converted-document chain the graph stage consumes
#
# The graph stage reads a conversion output, so seed one `Source` + `ConversionJob`
# + `ConversionOutput` per document (the foreign-key chain the real engine enforces)
# and a `LocalMarkdownSource` that returns each document's text by
# `conversion_output_id` — standing in for the production S3-backed source so the
# demo needs no object store while still driving the real handler, runner, and
# freshness gate.


# %%
@dataclass(frozen=True)
class SeededDoc:
    """A seeded document's durable + locator identities for the demo."""

    label: str
    aizk_uuid: UUID
    conversion_output_id: int
    markdown_hash: str


class LocalMarkdownSource:
    """An in-memory :class:`~aizk.graph.workunit.MarkdownSource` over seeded text.

    Returns the Markdown registered for a ``conversion_output_id``; stands in for
    the production S3-backed source so the demo runs without object storage.
    """

    def __init__(self) -> None:
        """Initialize an empty output-id-to-Markdown registry."""
        self._by_output: dict[int, object] = {}

    def register(self, conversion_output_id: int, text: str, markdown_hash: str) -> None:
        """Register a conversion output's Markdown text + recorded hash."""
        self._by_output[conversion_output_id] = LoadedMarkdown(text=text, markdown_hash_xx64=markdown_hash)

    def load(self, conversion_output_id: int):
        """Return the registered Markdown for a conversion output locator."""
        return self._by_output[conversion_output_id]


def seed_conversion_output(session, *, text: str, label: str, aizk_uuid: UUID) -> int:
    """Insert (or reuse) the source, then a new ConversionJob + ConversionOutput; return the output id.

    One :class:`Source` per ``aizk_uuid`` (a re-conversion of the same source adds
    a new job + output under the existing source), mirroring the real model.
    """
    markdown_hash = compute_markdown_hash(text)
    ref = f"demo://{aizk_uuid}"
    source = session.exec(select(Source).where(Source.aizk_uuid == aizk_uuid)).one_or_none()
    if source is None:
        source = Source(
            aizk_uuid=aizk_uuid,
            source_ref=ref,
            source_ref_hash=compute_markdown_hash(ref),
            owner_id="demo",
            title=label,
        )
        session.add(source)
        session.flush()
    job = ConversionJob(
        aizk_uuid=aizk_uuid,
        owner_id="demo",
        title=label,
        idempotency_key=f"demo-{uuid4().hex}",
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
        markdown_key=f"demo/{aizk_uuid}/output.md",
        manifest_key=f"demo/{aizk_uuid}/manifest.json",
        markdown_hash_xx64=markdown_hash,
        docling_version="demo",
        pipeline_name="docling",
    )
    session.add(output)
    session.flush()
    return output.id


def seed_documents(docs: list[DemoDoc]) -> tuple[list[SeededDoc], LocalMarkdownSource]:
    """Run migrations, seed each document's conversion-output chain, and build the source.

    Returns:
        The seeded-document specs and the local :class:`MarkdownSource` to inject.
    """
    run_migrations()
    engine = get_engine(DatabaseConfig().database_url)
    source = LocalMarkdownSource()
    seeded_docs: list[SeededDoc] = []
    with Session(engine) as session:
        for doc in docs:
            aizk_uuid = uuid4()
            output_id = seed_conversion_output(session, text=doc.markdown_text, label=doc.label, aizk_uuid=aizk_uuid)
            source.register(output_id, doc.markdown_text, compute_markdown_hash(doc.markdown_text))
            seeded_docs.append(
                SeededDoc(
                    label=doc.label,
                    aizk_uuid=aizk_uuid,
                    conversion_output_id=output_id,
                    markdown_hash=compute_markdown_hash(doc.markdown_text),
                )
            )
        session.commit()
    return seeded_docs, source


# %%
seeded, markdown_source = seed_documents(documents)
for doc in seeded:
    print(f"seeded {doc.label!r}: aizk_uuid={doc.aizk_uuid} conversion_output_id={doc.conversion_output_id}")

# %% [markdown]
# ## Build the worker and a runner driver
#
# `ContextualizationStageHandler` owns the unit of work (split, summarize,
# contextualize, persist) and the freshness gate; `StageRunner` is the engine that
# claims, executes, and finalizes work-units — together they are the exact path
# `aizk-graph worker` runs. `drain` drives the runner with `run_until_idle` so a
# cell returns once the queue empties instead of looping forever like the
# supervised worker.


# %%
def build_handler(source: LocalMarkdownSource, llm_client) -> ContextualizationStageHandler:
    """Build the real contextualization stage handler (with the real freshness gate) over the temp DB."""
    engine = get_engine(DatabaseConfig().database_url)
    return ContextualizationStageHandler(engine, llm_client, source, ConversionOutputFreshness())


def drain(stage_handler: ContextualizationStageHandler, *, max_iterations: int = 100_000) -> None:
    """Drive a :class:`StageRunner` over ``stage_handler`` until the queue drains."""
    engine = get_engine(DatabaseConfig().database_url)
    runner = StageRunner(
        stage_handler,
        engine=engine,
        poll_interval=0.05,
        stale_recovery_interval=3600.0,
        cancel_grace=2.0,
    )
    runner.run_until_idle(max_iterations=max_iterations)


# %%
handler = build_handler(markdown_source, client)
print(f"built handler over {DatabaseConfig().database_url}")

# %% [markdown]
# ## Enqueue each document as a contextualization work-unit
#
# `enqueue_output` registers one work-unit per conversion output, deduped on a
# derived idempotency key, and leaves it `queued` for the runner to claim. Nothing
# runs yet — this only inserts the queue rows the next cell drains.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    for doc in seeded:
        unit = enqueue_output(session, doc.conversion_output_id)
        print(f"enqueued {doc.label!r}: work-unit id={unit.id} status={unit.status.value}")
    session.commit()

# %% [markdown]
# ## Drain the worker — real model calls per chunk
#
# This is the live run: the runner claims each work-unit and the handler splits the
# document, calls the model once for the summary and once per chunk for the
# revisions, persists everything, and drives the lifecycle to a terminal state.
# Watch the `aizk` INFO logs — this is where the real endpoint is exercised.

# %%
drain(handler)
print("drained — all enqueued work-units reached a terminal state.")

# %% [markdown]
# ## Each work-unit reached a terminal status with a recorded event trail
#
# Read the lifecycle back: every work-unit carries a terminal `status` and an
# ordered `pipeline_events` trail (`queued → claimed → … → succeeded`) the operator
# surface renders. A `failed` unit shows its `error_code`/`error_message` here.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    for doc in seeded:
        job = session.exec(
            select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == doc.conversion_output_id)
        ).one()
        print(f"work-unit id={job.id} {doc.label!r} -> status={job.status.value} attempts={job.attempts}")
        if job.error_code or job.error_message:
            print(f"    error_code={job.error_code} error_message={job.error_message}")
        events = session.exec(
            select(PipelineEvent)
            .where(PipelineEvent.stage == "contextualization", PipelineEvent.work_unit_ref == str(job.id))
            .order_by(PipelineEvent.event_id)
        ).all()
        for event in events:
            print(f"    event {event.from_status} -> {event.to_status} kind={event.kind} run_id={event.run_id}")

# %% [markdown]
# ## Pick a successfully contextualized document to inspect
#
# The remaining cells walk one document's persisted outputs, so select the first
# work-unit that reached `succeeded`. If none did, the model preflight passed but
# per-document work failed — the `error_*` fields above say why.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    succeeded = [
        doc
        for doc in seeded
        if session.exec(
            select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == doc.conversion_output_id)
        )
        .one()
        .status
        == WorkUnitStatus.SUCCEEDED
    ]
if not succeeded:
    raise RuntimeError("No document processed successfully — see the error_code/error_message logged above.")
primary = succeeded[0]
print(f"inspecting {primary.label!r} (aizk_uuid={primary.aizk_uuid})")

# %% [markdown]
# ## Chunk persistence keeps stable identity, run-scoped span, and the consumed input
#
# Chunk identity carries only stable facts; the run's `manifest` carries each
# chunk's character `span`, and the run's `input` records which conversion output it
# consumed and that output's hash. Reconstructing `chunk ⋈ manifest ⋈ input ⋈ run`
# round-trips the emitted chunks — and the consumed hash equals the hash we seeded,
# proving the run read exactly the document we registered.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    run = active_chunking_run(session, str(primary.aizk_uuid))
    consumed = run_input(session, run.id)
    manifest = manifest_of_run(session, run.id)
    reconstructed = chunks_of_run(session, run.id)
    print(f"chunking run id={run.id}: {len(manifest)} chunks")
    print(
        f"  consumed conversion_output_id={consumed.conversion_output_id} markdown_hash={consumed.markdown_hash_xx64}"
    )
    print(f"  consumed hash == seeded hash -> {consumed.markdown_hash_xx64 == primary.markdown_hash}")
    # manifest is chunk_id-ordered; list in document order (by span) so it reads top-to-bottom.
    paired = sorted(zip(manifest, reconstructed, strict=True), key=lambda pair: pair[0].span_start)
    for entry, chunk in paired:
        preview = chunk.text.strip().replace("\n", " ")[:70]
        print(
            f"  chunk {entry.chunk_id[:12]} span=({entry.span_start},{entry.span_end}) "
            f"heading={chunk.heading_path} ord={chunk.ordinal}: {preview!r}"
        )

# %% [markdown]
# ## One run-scoped summary per document grounds the per-chunk passes
#
# Contextualization runs a single summary pass over the whole document; each chunk
# revision is conditioned on it. The summary row records the consumed
# `conversion_output_id` and its run's `derivation_key` — the input fingerprint that
# decides whether a re-run may reuse it.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    summary_run = session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == SUMMARY_STAGE,
            PipelineRun.scope_key == str(primary.aizk_uuid),
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one()
    summary = session.exec(select(DocumentSummary).where(DocumentSummary.run_id == summary_run.id)).one()
    print(f"summary run id={summary_run.id} version={summary.summary_version}")
    print(f"  derivation_key={summary_run.derivation_key}")
    print(f"  consumed conversion_output_id={summary.conversion_output_id}")
    print(f"  text: {summary.summary_text.strip()}")

# %% [markdown]
# ## Each chunk gets a self-contained revision; the raw chunk is untouched
#
# The model rewrites each chunk into a passage that stands on its own, resolving
# references inline using a 2-prior/1-next window plus the summary. An **empty**
# revision means the model judged the chunk already self-contained. The raw text is
# never modified; `resolve_chunk_text` selects raw vs revised at read time and tags
# which it returned. Watch the danglers ("this mechanism", "the approach described
# above") get resolved in the revised side.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    variant_run = session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == VARIANT_STAGE,
            PipelineRun.scope_key == str(primary.aizk_uuid),
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one()
    variants = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)).all()
    print(f"variant run id={variant_run.id}: {len(variants)} contextualized variants\n")
    for variant in variants[:3]:
        chunk = session.get(Chunk, variant.chunk_id)
        resolved = resolve_chunk_text(
            chunk.text, contextualized_text=variant.contextualized_text, contextualization_enabled=True
        )
        print(f"--- chunk {variant.chunk_id[:12]} ---")
        print(f"  raw     : {chunk.text.strip().replace(chr(10), ' ')}")
        print(f"  revised : {variant.contextualized_text.strip() or '<empty: already self-contained>'}")
        print(f"  resolve_chunk_text source={resolved.source.value}\n")

# %% [markdown]
# ## The raw chunk is never mutated — its content hash still matches its text
#
# Contextualization writes a *new* variant row; it does not touch the chunk. Proof:
# re-hash the stored raw text and compare it to the `content_hash` recorded at split
# time — they must still be equal after the contextualization run.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    variant_run = session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == VARIANT_STAGE,
            PipelineRun.scope_key == str(primary.aizk_uuid),
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one()
    sample_variant = session.exec(
        select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)
    ).first()
    sample_chunk = session.get(Chunk, sample_variant.chunk_id)
    recomputed = compute_markdown_hash(sample_chunk.text)
    print(f"stored content_hash : {sample_chunk.content_hash}")
    print(f"recomputed from text: {recomputed}")
    print(f"raw chunk unmodified -> {sample_chunk.content_hash == recomputed}")
    assert sample_chunk.content_hash == recomputed, "raw chunk text must be unmodified by contextualization"

# %% [markdown]
# ## Toggling contextualization off returns the raw text
#
# `resolve_chunk_text` is the read-time switch: with the toggle **off** it returns
# the raw chunk tagged `RAW`, regardless of whether a revision exists. That is the
# escape hatch for a consumer that wants the original text — proven by comparing the
# resolved text to the raw chunk side by side.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    variant_run = session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == VARIANT_STAGE,
            PipelineRun.scope_key == str(primary.aizk_uuid),
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one()
    sample_variant = session.exec(
        select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)
    ).first()
    sample_chunk = session.get(Chunk, sample_variant.chunk_id)
    off = resolve_chunk_text(
        sample_chunk.text,
        contextualized_text=sample_variant.contextualized_text,
        contextualization_enabled=False,
    )
    print(f"toggle off -> source={off.source.value}")
    print(f"resolved text == raw chunk text -> {off.text == sample_chunk.text}")
    assert off.source is ContextSource.RAW and off.text == sample_chunk.text

# %% [markdown]
# ## A variant traces backward to its source, hash-verified at each edge
#
# Provenance runs the other way too: from one variant recover the chunking
# generation it read, that chunk's span and text (hash-verified), the source
# Markdown it consumed, the summary it used, and finally the single `aizk_uuid` the
# whole chain belongs to — each edge asserted out loud so a broken link is visible.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    variant_run = session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == VARIANT_STAGE,
            PipelineRun.scope_key == str(primary.aizk_uuid),
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one()
    variant = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)).first()
    print(f"tracing variant id={variant.id} (chunk {variant.chunk_id[:12]}) backward:")

    chunking = session.get(PipelineRun, variant.chunking_run_id)
    print(f"  1. chunking_run_id={chunking.id} (stage={chunking.stage}, scope={chunking.scope_key})")

    entry = next(m for m in manifest_of_run(session, chunking.id) if m.chunk_id == variant.chunk_id)
    chunk = session.get(Chunk, variant.chunk_id)
    chunk_text_ok = chunk.content_hash == compute_markdown_hash(chunk.text)
    print(f"  2. manifest span=({entry.span_start},{entry.span_end}); chunk text hash-verified -> {chunk_text_ok}")
    assert chunk_text_ok

    consumed = run_input(session, chunking.id)
    print(
        f"  3. consumed conversion_output_id={consumed.conversion_output_id} markdown_hash={consumed.markdown_hash_xx64}"
    )

    summary_run = session.get(PipelineRun, variant.summary_run_id)
    print(f"  4. summary_run_id={summary_run.id} stage={summary_run.stage}")
    assert summary_run.stage == SUMMARY_STAGE

    chain_agrees = chunking.scope_key == str(primary.aizk_uuid) == summary_run.scope_key == chunk.doc_id
    print(f"  5. aizk_uuid={primary.aizk_uuid}; chunk.doc_id + chunking + summary scopes all agree -> {chain_agrees}")
    assert chain_agrees

# %% [markdown]
# ## Bulk and incremental enqueue converge on one work-unit
#
# Both enqueue modes derive the same idempotency key, so enqueuing the same
# conversion output incrementally and via a bulk backfill returns the **same**
# work-unit — one write path, one record set, by construction. The proof is
# `incremental.id == bulk.id` with exactly one matching unit in the table.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    incremental = enqueue_output(session, primary.conversion_output_id)
    (bulk,) = enqueue_backfill_outputs(session, [primary.conversion_output_id])
    session.commit()
    units = session.exec(
        select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == primary.conversion_output_id)
    ).all()
print(f"incremental unit id={incremental.id}, bulk unit id={bulk.id}, distinct units={len(units)}")
print(f"incremental.id == bulk.id and one unit -> {incremental.id == bulk.id and len(units) == 1}")
assert incremental.id == bulk.id and len(units) == 1

# %% [markdown]
# ## Supersession + monotonic currentness
#
# Re-converting a source produces a newer conversion output; processing it
# supersedes the prior generation's runs so "current" advances. A late **older**
# output must never drag current back to stale text — the freshness gate skips it.
#
# The state this scenario moves is the source's set of **chunking runs** in
# `pipeline_runs`: each run has a `status` (`ACTIVE` / `SUPERSEDED`) and consumes
# one conversion output. `chunking_runs` snapshots that whole set for a source;
# printing it **before and after** each step makes the transition legible — you see
# exactly which run flips `ACTIVE → SUPERSEDED`, which new `ACTIVE` run appears,
# and (in the late-older case) that nothing changed at all. The single invariant
# across every state: **exactly one run is `ACTIVE`**, and it consumes the newest
# output ever processed.

# %%
_SUPERSEDE_OLD = "# Note\n\nThe first revision of a short note, with one section.\n\n## Detail\n\nIt explains the original point in a single paragraph.\n"
_SUPERSEDE_NEW = "# Note\n\nThe second revision of a short note, expanded.\n\n## Detail\n\nIt explains the revised point, now with additional clarifying material.\n"
supersede_uuid = uuid4()


@dataclass(frozen=True)
class RunState:
    """One chunking run's observable state: its id, lifecycle status, and consumed output."""

    run_id: int
    status: str
    consumes_output: int


def chunking_runs(aizk_uuid: UUID) -> list[RunState]:
    """Snapshot every chunking run for a source, in creation order.

    Returns one :class:`RunState` per run — ``(run_id, status, consumed output)`` —
    so a caller can diff the snapshot across a step to see which run changed status
    and which appeared. ``ChunkRunInput.conversion_output_id`` is the *stringified*
    locator (the schema is decoupled from the conversion stage's PK type); convert
    back to ``int`` here, the boundary that dereferences it, so it compares to the
    ``ConversionOutput.id`` integers the seed helpers return.
    """
    engine = get_engine(DatabaseConfig().database_url)
    with Session(engine) as session:
        runs = session.exec(
            select(PipelineRun)
            .where(PipelineRun.stage == "chunking", PipelineRun.scope_key == str(aizk_uuid))
            .order_by(PipelineRun.id)
        ).all()
        return [
            RunState(run.id, run.status.value, int(run_input(session, run.id).conversion_output_id)) for run in runs
        ]


def show_runs(label: str, runs: list[RunState]) -> None:
    """Print a chunking-run snapshot as an aligned table under ``label``."""
    print(label)
    if not runs:
        print("  (no chunking runs yet)")
    for run in runs:
        print(f"  run {run.run_id}: status={run.status:<10} consumes output {run.consumes_output}")


def active_consumes(aizk_uuid: UUID) -> int:
    """Return the conversion_output_id the source's single active chunking run consumed."""
    active = [run for run in chunking_runs(aizk_uuid) if run.status == RunStatus.ACTIVE.value]
    if len(active) != 1:
        raise AssertionError(f"expected exactly one ACTIVE chunking run, found {len(active)}")
    return active[0].consumes_output


# %% [markdown]
# ### The first conversion output becomes the active generation
#
# Before: the source has no chunking runs. Seed and process revision 1; after, a
# single `ACTIVE` run exists, consuming rev 1's output. This is the baseline the
# next step supersedes.

# %%
show_runs("before rev 1:", chunking_runs(supersede_uuid))
with Session(get_engine(DatabaseConfig().database_url)) as session:
    old_output = seed_conversion_output(session, text=_SUPERSEDE_OLD, label="note (rev 1)", aizk_uuid=supersede_uuid)
    session.commit()
markdown_source.register(old_output, _SUPERSEDE_OLD, compute_markdown_hash(_SUPERSEDE_OLD))
with Session(get_engine(DatabaseConfig().database_url)) as session:
    enqueue_output(session, old_output)
    session.commit()
drain(handler)
show_runs(f"after rev 1 (output {old_output}):", chunking_runs(supersede_uuid))
assert active_consumes(supersede_uuid) == old_output

# %% [markdown]
# ### A re-conversion supersedes the prior generation
#
# Seed and process revision 2 (a newer output, changed Markdown). Diff the
# before/after snapshots: rev 1's run flips `ACTIVE → SUPERSEDED` **in place** (same
# `run_id`, it is never deleted) and a new `ACTIVE` run appears consuming rev 2.
# Current has advanced, and the superseded run is still on record for provenance.

# %%
before = chunking_runs(supersede_uuid)
show_runs("before rev 2:", before)
with Session(get_engine(DatabaseConfig().database_url)) as session:
    new_output = seed_conversion_output(session, text=_SUPERSEDE_NEW, label="note (rev 2)", aizk_uuid=supersede_uuid)
    session.commit()
markdown_source.register(new_output, _SUPERSEDE_NEW, compute_markdown_hash(_SUPERSEDE_NEW))
with Session(get_engine(DatabaseConfig().database_url)) as session:
    enqueue_output(session, new_output)
    session.commit()
drain(handler)
after = chunking_runs(supersede_uuid)
show_runs(f"after rev 2 (output {new_output}):", after)
# The prior ACTIVE run is the same row, now SUPERSEDED — supersession is a status
# flip, not a delete or a rewrite.
(rev1_run,) = before
print(
    f"\nrev 1 run {rev1_run.run_id}: {rev1_run.status} -> {next(r.status for r in after if r.run_id == rev1_run.run_id)}"
)
print(f"active now consumes rev 2 -> {active_consumes(supersede_uuid) == new_output}")
assert active_consumes(supersede_uuid) == new_output
assert [r for r in after if r.run_id == rev1_run.run_id][0].status == RunStatus.SUPERSEDED.value

# %% [markdown]
# ### A late older output cannot drag current backward
#
# Re-queue revision 1 as if it arrived late. The freshness gate sees a newer output
# already active and skips it — so the **snapshot is unchanged**: same runs, same
# statuses, same active output. Nothing moved; this is the no-op the gate
# guarantees, shown by an identical before/after table.

# %%
before = chunking_runs(supersede_uuid)
show_runs("before late rev 1:", before)
with Session(get_engine(DatabaseConfig().database_url)) as session:
    old_job = session.exec(
        select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == old_output)
    ).one()
    old_job.status = WorkUnitStatus.QUEUED
    session.add(old_job)
    session.commit()
drain(handler)
after = chunking_runs(supersede_uuid)
show_runs("after late rev 1:", after)
print(f"\nstate unchanged -> {before == after}; active still consumes rev 2 ({new_output})")
assert before == after
assert active_consumes(supersede_uuid) == new_output

# %% [markdown]
# ## Operator surface: list, detail, retry, cancel
#
# The stage owns a FastAPI surface over the same database an operator uses to drive
# stuck work. Seed two units in known states (a retryable `failed` and a `queued`),
# then open a `TestClient` (its lifespan loads the shared config onto `app.state`).
# The `ExitStack` keeps the client open across the cells below; the Cleanup cell
# closes it.

# %%
with Session(get_engine(DatabaseConfig().database_url)) as session:
    failed_unit = ContextualizationJob(
        idempotency_key=f"demo-op-failed-{uuid4().hex}",
        conversion_output_id=900001,
        aizk_uuid=uuid4(),
        status=WorkUnitStatus.FAILED,
        attempts=3,
        error_code="demo_error",
    )
    queued_unit = ContextualizationJob(
        idempotency_key=f"demo-op-queued-{uuid4().hex}",
        conversion_output_id=900002,
        aizk_uuid=uuid4(),
        status=WorkUnitStatus.QUEUED,
    )
    session.add(failed_unit)
    session.add(queued_unit)
    session.commit()
    failed_id, queued_id = failed_unit.id, queued_unit.id

_op_stack = ExitStack()
op_client = _op_stack.enter_context(TestClient(create_app(), base_url="http://localhost"))
print(f"seeded operator units: failed id={failed_id}, queued id={queued_id}")

# %% [markdown]
# ### List enumerates every work-unit
#
# `GET /v1/contextualizations` returns the paged list the jobs monitor renders, with
# a `total` count across all statuses.

# %%
listed = op_client.get("/v1/contextualizations").json()
print(f"list -> total={listed['total']}, returned {len(listed['jobs'])} job(s)")
for item in listed["jobs"]:
    print(f"  id={item['id']} status={item['status']} conversion_output_id={item['conversion_output_id']}")

# %% [markdown]
# ### Detail returns one unit's full record
#
# `GET /v1/contextualizations/{id}` is the drill-down: the failed unit shows its
# `error_code` and attempt count, the data an operator reads before deciding to
# retry.

# %%
detail = op_client.get(f"/v1/contextualizations/{failed_id}").json()
print(
    f"detail id={detail['id']} status={detail['status']} attempts={detail['attempts']} error_code={detail['error_code']}"
)

# %% [markdown]
# ### Retry re-queues a failed unit
#
# `POST /{id}/retry` moves the failed unit back to `queued` so the runner will pick
# it up again — the before/after status is the proof.

# %%
print(f"before retry: status={op_client.get(f'/v1/contextualizations/{failed_id}').json()['status']}")
retried = op_client.post(f"/v1/contextualizations/{failed_id}/retry").json()
print(f"after retry : status={retried['status']}")

# %% [markdown]
# ### Cancel stops a queued unit
#
# `POST /{id}/cancel` moves the queued unit to `cancelled` so the runner will not
# claim it — again shown before/after.

# %%
print(f"before cancel: status={op_client.get(f'/v1/contextualizations/{queued_id}').json()['status']}")
cancelled = op_client.post(f"/v1/contextualizations/{queued_id}/cancel").json()
print(f"after cancel : status={cancelled['status']}")

# %% [markdown]
# ## At-least-once execution is idempotent
#
# The runner guarantees at-least-once, so a completed unit may be re-claimed (as
# stale recovery would). Re-running `primary` must reuse its active runs and write
# **no** duplicate rows — the proof is identical (runs, summaries, variants) counts
# before and after.


# %%
def row_counts() -> tuple[int, int, int]:
    """Return (PipelineRun, DocumentSummary, ContextualizedChunk) row counts in the temp DB."""
    engine = get_engine(DatabaseConfig().database_url)
    with Session(engine) as session:
        return (
            len(session.exec(select(PipelineRun)).all()),
            len(session.exec(select(DocumentSummary)).all()),
            len(session.exec(select(ContextualizedChunk)).all()),
        )


before_counts = row_counts()
with Session(get_engine(DatabaseConfig().database_url)) as session:
    job = session.exec(
        select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == primary.conversion_output_id)
    ).one()
    job.status = WorkUnitStatus.QUEUED
    session.add(job)
    session.commit()
drain(handler)
after_counts = row_counts()
print(f"counts before re-run = {before_counts} (runs, summaries, variants)")
print(f"counts after  re-run = {after_counts}")
print(f"no duplicate rows -> {before_counts == after_counts}")
assert before_counts == after_counts, "re-execution must not create duplicate runs or rows"

# %% [markdown]
# ## Where this lives
#
# The unit of work is `aizk.graph.handler.ContextualizationStageHandler` driven by
# `aizk.pipeline.runner.StageRunner`; the model passes and the read-time toggle are
# in `aizk.graph.contextualization` (`generate_summary_text`, `generate_revisions`,
# `resolve_chunk_text`); persistence and the provenance joins are in
# `aizk.graph.persistence`; the freshness/supersession gate is in
# `aizk.graph.markdown_source`; the operator API is in `aizk.graph.api`. Worked
# examples live in `tests/graph/`.

# %% [markdown]
# ## Cleanup
#
# Run this when you are done: close the operator client and remove the temp
# database directory.

# %%
_op_stack.close()
shutil.rmtree(tmp_db.parent, ignore_errors=True)
print(f"closed operator client and removed {tmp_db.parent}")
