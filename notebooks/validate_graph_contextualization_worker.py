#!/usr/bin/env python3
"""Real-world end-to-end demo for the graph contextualization worker.

This is a MANUAL, runnable user demo (NOT a pytest test). It drives the
runner-based graph stage — :class:`aizk.graph.handler.ContextualizationStageHandler`
under :class:`aizk.pipeline.runner.StageRunner`, the path ``aizk-graph worker``
runs — over a small sample of converted documents, into a THROWAWAY temporary
SQLite database. The default ``data/conversion_service.db`` is never touched.

It performs REAL model work: the summary and per-chunk contextualization passes
call the configured OpenAI-compatible endpoint, so you see genuine summaries and
genuine self-contained revisions (references resolved inline). It then walks the
persisted outputs at every stage and the full provenance / backward-trace chain,
showing off the change's user stories:

* chunk persistence with complete fidelity (stable identity + manifest + input);
* the document summary and the per-chunk self-contained revision;
* the raw-vs-contextualized resolve-at-use toggle;
* backward traceability (variant -> chunking generation -> chunk + span -> source
  markdown -> ``aizk_uuid``), hash-verified at each edge;
* run-mode independence (bulk and incremental enqueue dedupe to one write path);
* source-scoped supersession on re-conversion;
* monotonic currentness (a late older conversion output cannot supersede a newer
  one's runs);
* the operator surface (list / detail / retry / cancel);
* at-least-once idempotency of the work-unit.

Inputs are reused from a prior Docling run (``data/validate_docling_worker/*/output.md``)
when present; otherwise a single small vendored sample document is used so the
demo always runs. The supersession / currentness / operator scenarios use small
synthetic documents so they stay fast and self-contained.

Required environment (set in your shell or ``.env``):

* ``AIZK_GRAPH__CONTEXTUALIZATION__LLM_BASE_URL`` — OpenAI-compatible endpoint.
* ``AIZK_GRAPH__CONTEXTUALIZATION__LLM_API_KEY`` — its API key.
* ``AIZK_GRAPH__CONTEXTUALIZATION__LLM_MODEL`` — the model id.

The cells below fail fast with a clear message if the endpoint is not configured.

Run as a Jupyter-style ``# %%`` notebook (VS Code / Jupytext) and execute top to
bottom. Importing/parsing this module is side-effect-free: the temp-DB setup and
the live run live under the ``__main__`` guard, so the file can be statically
validated without doing any real work. Do NOT run it in CI or the sandbox.
"""

# %%
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid4

# %% [markdown]
# ## 1. Isolate to a throwaway temp database (run FIRST)
#
# `AIZK_DATABASE_URL` MUST be set before any `ConversionConfig` / engine is
# constructed, because every config reads the URL at construction time and
# `aizk.conversion.db.get_engine` caches engines by URL. We point it at a fresh
# temp SQLite file so the real `data/conversion_service.db` is never touched,
# then guard that the active config really resolved to the temp file.

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

    from aizk.conversion.utilities.config import ConversionConfig

    active_url = ConversionConfig().database_url
    if active_url != tmp_url:
        raise RuntimeError(
            f"Temp-DB isolation failed: ConversionConfig().database_url is {active_url!r}, "
            f"expected {tmp_url!r}. Refusing to run against a non-temp database."
        )
    if active_url == DEFAULT_DB_URL or active_url.endswith("conversion_service.db"):
        raise RuntimeError(f"Active database URL {active_url!r} points at the default service DB. Refusing to run.")
    return tmp_db, tmp_url


# %% [markdown]
# ## 2. Logging + the required contextualization model
#
# The demo calls a real model for the summary and per-chunk revisions; fail fast
# with a clear message if the OpenAI-compatible endpoint is not configured.


# %%
def configure_demo_logging() -> logging.Logger:
    """Configure INFO logging for the demo and return this module's logger."""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("aizk").setLevel(logging.INFO)
    log = logging.getLogger("graph_demo")
    log.setLevel(logging.INFO)
    return log


def build_demo_llm_client(log: logging.Logger):
    """Build the real contextualization model client, echo its resolved config, and preflight it.

    Logs the resolved endpoint/model (api key masked) so a stray or unexpanded
    ``${...}`` ``.env`` substitution is obvious, then issues one trivial model call
    so a misconfigured endpoint/model/key fails *here* with the real provider
    error rather than as opaque per-document work-unit failures later.

    Returns:
        The injected :class:`~aizk.graph.llm.LLMClient` backed by ``pydantic-ai``.

    Raises:
        RuntimeError: If the ``AIZK_GRAPH__CONTEXTUALIZATION__LLM_*`` triple is not
            fully set, or the preflight model call fails.
    """
    from aizk.conversion.utilities.startup import StartupValidationError
    from aizk.graph.config import ContextualizationConfig
    from aizk.graph.worker import build_llm_client

    config = ContextualizationConfig()
    key = config.llm_api_key
    masked = f"set (len={len(key)})" if key else "MISSING"
    log.info(
        "contextualization model: base_url=%r model=%r api_key=%s",
        config.llm_base_url or "MISSING",
        config.llm_model or "MISSING",
        masked,
    )
    if "${" in (config.llm_base_url + config.llm_api_key + config.llm_model):
        raise RuntimeError(
            "A model setting still contains a literal '${...}' substitution: the .env interpolation did not reach "
            "this kernel. Either define the referent vars (e.g. _OPENROUTER_BASE_URL) ABOVE these lines in .env so "
            "python-dotenv expands them from the file, or set "
            "AIZK_GRAPH__CONTEXTUALIZATION__LLM_BASE_URL / _API_KEY / _MODEL to literal values, then restart the kernel."
        )

    try:
        client = build_llm_client(config)
    except StartupValidationError as exc:
        raise RuntimeError(f"{exc}\nVerify with:  env | grep AIZK_GRAPH__CONTEXTUALIZATION__") from exc

    log.info("preflighting the model with one call...")
    try:
        reply = client.generate("Reply with the single word: OK")
    except Exception as exc:
        raise RuntimeError(
            f"Preflight model call failed against base_url={config.llm_base_url!r} model={config.llm_model!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    log.info("preflight OK; model replied %r", reply.strip()[:80])
    return client


# %% [markdown]
# ## 3. Load the sample documents
#
# Reuse a prior Docling run's `output.md` files when present; otherwise fall back
# to one small vendored document. `SAMPLE_SIZE` bounds how many documents the
# worker processes — each one drives a real summary call plus one revision call
# per chunk, so keep it small.

# %%
SAMPLE_SIZE = 2
DOCLING_OUTPUTS_DIR = Path("data/validate_docling_worker")

# A small, self-contained fallback whose cross-references (pronouns, "the approach
# described above", ...) a real model resolves inline in the contextualized
# revision — so the raw-vs-revised contrast is visible even with no prior run.
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


# %% [markdown]
# ## 4. Seed the converted documents and a Markdown source
#
# The graph stage consumes conversion outputs. Seed one `Source` + `ConversionJob`
# + `ConversionOutput` per document (the FK chain the real engine enforces), and a
# local `MarkdownSource` that returns each document's text by locator — so the
# demo needs no S3 while using the real handler, runner, and freshness gate.


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
        self._by_output: dict[int, object] = {}

    def register(self, conversion_output_id: int, text: str, markdown_hash: str) -> None:
        """Register a conversion output's Markdown text + recorded hash."""
        from aizk.graph.workunit import LoadedMarkdown

        self._by_output[conversion_output_id] = LoadedMarkdown(text=text, markdown_hash_xx64=markdown_hash)

    def load(self, conversion_output_id: int):
        """Return the registered Markdown for a conversion output locator."""
        return self._by_output[conversion_output_id]


def _seed_conversion_output(session, *, text: str, label: str, aizk_uuid: UUID) -> int:
    """Insert (or reuse) the source, then a new ConversionJob + ConversionOutput; return the output id.

    One :class:`Source` per ``aizk_uuid`` (a re-conversion of the same source adds
    a new job + output under the existing source), mirroring the real model.
    """
    from sqlmodel import select

    from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
    from aizk.conversion.datamodel.output import ConversionOutput
    from aizk.conversion.datamodel.source import Source
    from aizk.utilities.hashing import compute_markdown_hash

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


def seed_documents(documents: list[DemoDoc]) -> tuple[list[SeededDoc], LocalMarkdownSource]:
    """Run migrations, seed each document's conversion-output chain, and build the source.

    Returns:
        The seeded-document specs and the local :class:`MarkdownSource` to inject.
    """
    from sqlmodel import Session

    from aizk.conversion.db import get_engine
    from aizk.conversion.migrations import run_migrations
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.utilities.hashing import compute_markdown_hash

    run_migrations()
    engine = get_engine(ConversionConfig().database_url)
    markdown_source = LocalMarkdownSource()
    seeded: list[SeededDoc] = []
    with Session(engine) as session:
        for doc in documents:
            aizk_uuid = uuid4()
            output_id = _seed_conversion_output(session, text=doc.markdown_text, label=doc.label, aizk_uuid=aizk_uuid)
            markdown_source.register(output_id, doc.markdown_text, compute_markdown_hash(doc.markdown_text))
            seeded.append(
                SeededDoc(
                    label=doc.label,
                    aizk_uuid=aizk_uuid,
                    conversion_output_id=output_id,
                    markdown_hash=compute_markdown_hash(doc.markdown_text),
                )
            )
        session.commit()
    return seeded, markdown_source


# %% [markdown]
# ## 5. Build the worker and drain the sample
#
# Build the real `ContextualizationStageHandler` (with the real freshness gate)
# under a `StageRunner`, enqueue each document incrementally, and drive
# `run_until_idle()` so the cell returns once the sample drains.


# %%
def build_handler(markdown_source, llm_client):
    """Build the real contextualization stage handler over the temp DB."""
    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.handler import ContextualizationStageHandler
    from aizk.graph.markdown_source import ConversionOutputFreshness

    engine = get_engine(ConversionConfig().database_url)
    return ContextualizationStageHandler(engine, llm_client, markdown_source, ConversionOutputFreshness())


def enqueue_documents(seeded: list[SeededDoc]) -> None:
    """Enqueue each seeded document as a contextualization work-unit (incremental mode)."""
    from sqlmodel import Session

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.enqueue import enqueue_output

    engine = get_engine(ConversionConfig().database_url)
    with Session(engine) as session:
        for doc in seeded:
            enqueue_output(session, doc.conversion_output_id)
        session.commit()


def drain_worker(handler, *, max_iterations: int = 100_000) -> None:
    """Drive the StageRunner over ``handler`` until the queue drains."""
    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.pipeline.runner import StageRunner

    engine = get_engine(ConversionConfig().database_url)
    runner = StageRunner(handler, engine, poll_interval=0.05, stale_recovery_interval=3600.0, cancel_grace=2.0)
    runner.run_until_idle(max_iterations=max_iterations)


def report_lifecycle(log: logging.Logger, seeded: list[SeededDoc]) -> None:
    """Print each work-unit's terminal status and its transition events."""
    from sqlmodel import Session, select

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.datamodel import ContextualizationJob
    from aizk.pipeline.events import PipelineEvent

    engine = get_engine(ConversionConfig().database_url)
    with Session(engine) as session:
        for doc in seeded:
            job = session.exec(
                select(ContextualizationJob).where(
                    ContextualizationJob.conversion_output_id == doc.conversion_output_id
                )
            ).one()
            log.info("work-unit id=%s %r -> status=%s attempts=%s", job.id, doc.label, job.status.value, job.attempts)
            if job.error_code or job.error_message:
                log.warning("    error_code=%s error_message=%s", job.error_code, job.error_message)
            events = session.exec(
                select(PipelineEvent)
                .where(PipelineEvent.stage == "contextualization", PipelineEvent.work_unit_ref == str(job.id))
                .order_by(PipelineEvent.event_id)
            ).all()
            for event in events:
                log.info(
                    "    event %s -> %s kind=%s run_id=%s aizk_uuid=%s",
                    event.from_status,
                    event.to_status,
                    event.kind,
                    event.run_id,
                    event.aizk_uuid,
                )


def unit_status(doc: SeededDoc) -> str:
    """Return the current lifecycle status value of a seeded document's work-unit."""
    from sqlmodel import Session, select

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.datamodel import ContextualizationJob

    engine = get_engine(ConversionConfig().database_url)
    with Session(engine) as session:
        job = session.exec(
            select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == doc.conversion_output_id)
        ).one()
        return job.status.value


# %% [markdown]
# ## 6. Chunk persistence with complete fidelity
#
# The chunk identity carries only stable facts; the `chunk_run_manifest` carries
# each chunk's `span` and the `chunk_run_input` records the consumed output + hash.
# Reconstructing `chunk join manifest join input join run` round-trips the emitted
# chunk field-for-field.


# %%
def show_chunks(log: logging.Logger, doc: SeededDoc) -> None:
    """Print the active chunking run's identities, manifest spans, and input."""
    from sqlmodel import Session

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.persistence import active_chunking_run, chunks_of_run, manifest_of_run, run_input

    engine = get_engine(ConversionConfig().database_url)
    with Session(engine) as session:
        run = active_chunking_run(session, str(doc.aizk_uuid))
        consumed = run_input(session, run.id)
        manifest = manifest_of_run(session, run.id)
        reconstructed = chunks_of_run(session, run.id)
        log.info("Document %r: chunking run id=%s, %d chunks", doc.label, run.id, len(manifest))
        log.info(
            "  consumed input: conversion_output_id=%s markdown_hash=%s",
            consumed.conversion_output_id,
            consumed.markdown_hash_xx64,
        )
        # The manifest is chunk_id-ordered; list it in document order (by span) so
        # the sections read top-to-bottom.
        paired = sorted(zip(manifest, reconstructed, strict=True), key=lambda pair: pair[0].span_start)
        for entry, chunk in paired:
            preview = chunk.text.strip().replace("\n", " ")[:70]
            log.info(
                "  chunk %s span=(%s,%s) heading=%s ordinal=%s : %r",
                entry.chunk_id[:12],
                entry.span_start,
                entry.span_end,
                chunk.heading_path,
                chunk.ordinal,
                preview,
            )


# %% [markdown]
# ## 7. The document summary
#
# One run-scoped summary per document, recording the consumed output and the
# derivation key that produced it.


# %%
def show_summary(log: logging.Logger, doc: SeededDoc) -> None:
    """Print the active document summary and its run derivation key."""
    from sqlmodel import Session, select

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.contextualization import SUMMARY_STAGE
    from aizk.graph.datamodel import DocumentSummary
    from aizk.pipeline.run import PipelineRun, RunStatus

    engine = get_engine(ConversionConfig().database_url)
    with Session(engine) as session:
        run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == SUMMARY_STAGE,
                PipelineRun.scope_key == str(doc.aizk_uuid),
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        summary = session.exec(select(DocumentSummary).where(DocumentSummary.run_id == run.id)).one()
        log.info("Document %r summary (run id=%s, version=%s):", doc.label, run.id, summary.summary_version)
        log.info("  derivation_key=%s", run.derivation_key)
        log.info("  consumed conversion_output_id=%s", summary.conversion_output_id)
        log.info("  text: %s", summary.summary_text.strip())


# %% [markdown]
# ## 8. The contextualized variant + resolve-at-use toggle
#
# Each chunk gets a self-contained revision (references resolved inline). The raw
# chunk is never modified; `resolve_chunk_text` selects raw vs revised at use time.


# %%
def show_variants(log: logging.Logger, doc: SeededDoc, *, limit: int = 3) -> None:
    """Print raw chunk text vs its self-contained revision for a few chunks."""
    from sqlmodel import Session, select

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.contextualization import VARIANT_STAGE, ContextSource, resolve_chunk_text
    from aizk.graph.datamodel import Chunk, ContextualizedChunk
    from aizk.pipeline.run import PipelineRun, RunStatus

    engine = get_engine(ConversionConfig().database_url)
    with Session(engine) as session:
        run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == VARIANT_STAGE,
                PipelineRun.scope_key == str(doc.aizk_uuid),
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        variants = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == run.id)).all()
        log.info("Document %r: %d contextualized variants (run id=%s)", doc.label, len(variants), run.id)
        for variant in variants[:limit]:
            chunk = session.get(Chunk, variant.chunk_id)
            resolved = resolve_chunk_text(
                chunk.text, contextualized_text=variant.contextualized_text, contextualization_enabled=True
            )
            log.info("  --- chunk %s ---", variant.chunk_id[:12])
            log.info("    raw       : %s", chunk.text.strip().replace("\n", " "))
            log.info("    revised   : %s", (variant.contextualized_text.strip() or "<empty: already self-contained>"))
            log.info("    resolved-at-use source=%s", resolved.source.value)
        # The raw chunk is unchanged: its content hash still matches its text.
        from aizk.utilities.hashing import compute_markdown_hash

        sample = session.get(Chunk, variants[0].chunk_id)
        assert sample.content_hash == compute_markdown_hash(sample.text), "raw chunk text must be unmodified"
        # Toggling contextualization off yields the raw text.
        off = resolve_chunk_text(
            sample.text, contextualized_text=variants[0].contextualized_text, contextualization_enabled=False
        )
        assert off.source is ContextSource.RAW and off.text == sample.text
        log.info("  raw chunk unchanged after contextualization; toggle off -> raw text")


# %% [markdown]
# ## 9. Backward traceability
#
# From a variant, recover one hash-verifiable edge at a time: the chunking
# generation it read, that chunk's `span` + text, the source markdown it consumed,
# and the `aizk_uuid` the chain belongs to.


# %%
def trace_backward(log: logging.Logger, doc: SeededDoc) -> None:
    """Walk a variant back to its source identity, verifying each edge."""
    from sqlmodel import Session, select

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.contextualization import SUMMARY_STAGE, VARIANT_STAGE
    from aizk.graph.datamodel import Chunk, ContextualizedChunk
    from aizk.graph.persistence import manifest_of_run, run_input
    from aizk.pipeline.run import PipelineRun, RunStatus
    from aizk.utilities.hashing import compute_markdown_hash

    engine = get_engine(ConversionConfig().database_url)
    with Session(engine) as session:
        variant_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == VARIANT_STAGE,
                PipelineRun.scope_key == str(doc.aizk_uuid),
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        variant = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)).first()
        log.info("Tracing variant id=%s (chunk %s) backward:", variant.id, variant.chunk_id[:12])

        # 1. variant -> the chunking generation it read.
        chunking = session.get(PipelineRun, variant.chunking_run_id)
        log.info("  1. chunking_run_id=%s (stage=%s, scope=%s)", chunking.id, chunking.stage, chunking.scope_key)

        # 2. generation -> manifest (chunk_id, span) + chunk text (hash-verified).
        entry = next(m for m in manifest_of_run(session, chunking.id) if m.chunk_id == variant.chunk_id)
        chunk = session.get(Chunk, variant.chunk_id)
        assert chunk.content_hash == compute_markdown_hash(chunk.text)
        log.info("  2. manifest span=(%s,%s); chunk text hash-verified", entry.span_start, entry.span_end)

        # 3. generation -> consumed source markdown (locator + verifiable hash).
        consumed = run_input(session, chunking.id)
        log.info(
            "  3. consumed conversion_output_id=%s markdown_hash=%s",
            consumed.conversion_output_id,
            consumed.markdown_hash_xx64,
        )

        # 4. the summary it used.
        summary_run = session.get(PipelineRun, variant.summary_run_id)
        assert summary_run.stage == SUMMARY_STAGE
        log.info("  4. summary_run_id=%s", summary_run.id)

        # 5. the whole chain belongs to one aizk_uuid.
        assert chunking.scope_key == str(doc.aizk_uuid) == summary_run.scope_key == chunk.doc_id
        log.info("  5. aizk_uuid=%s (chunk.doc_id, chunking + summary scope all agree)", doc.aizk_uuid)


# %% [markdown]
# ## 10. Run-mode independence (bulk and incremental feed one write path)
#
# Both enqueue modes dedupe on the work-unit `idempotency_key`, so enqueuing the
# same conversion output incrementally and via a bulk backfill converge on the
# **same** work-unit — one write path, one record set, by construction.


# %%
def demo_run_mode_independence(log: logging.Logger, doc: SeededDoc) -> None:
    """Show incremental and bulk enqueue of the same output reuse one work-unit."""
    from sqlmodel import Session, select

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.datamodel import ContextualizationJob
    from aizk.graph.enqueue import enqueue_backfill_outputs, enqueue_output

    engine = get_engine(ConversionConfig().database_url)
    with Session(engine) as session:
        incremental = enqueue_output(session, doc.conversion_output_id)
        (bulk,) = enqueue_backfill_outputs(session, [doc.conversion_output_id])
        session.commit()
        units = session.exec(
            select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == doc.conversion_output_id)
        ).all()
    log.info(
        "incremental unit id=%s, bulk unit id=%s, distinct units=%d -> one write path",
        incremental.id,
        bulk.id,
        len(units),
    )
    assert incremental.id == bulk.id and len(units) == 1


# %% [markdown]
# ## 11. Source-scoped supersession + monotonic currentness
#
# Re-converting a source (a newer `ConversionOutput`, changed Markdown) supersedes
# the prior generation's runs. A late *older* output is then skipped — it can never
# drag "current" back to stale text.


# %%
_SUPERSEDE_OLD = "# Note\n\nThe first revision of a short note, with one section.\n\n## Detail\n\nIt explains the original point in a single paragraph.\n"
_SUPERSEDE_NEW = "# Note\n\nThe second revision of a short note, expanded.\n\n## Detail\n\nIt explains the revised point, now with additional clarifying material.\n"


def demo_supersession_and_currentness(log: logging.Logger, handler, markdown_source: LocalMarkdownSource) -> None:
    """Process an older output, supersede it with a re-conversion, then run the older one late."""
    from sqlmodel import Session, select

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.datamodel import ContextualizationJob
    from aizk.graph.enqueue import enqueue_output
    from aizk.graph.persistence import active_chunking_run, run_input
    from aizk.pipeline.lifecycle import WorkUnitStatus
    from aizk.pipeline.run import PipelineRun, RunStatus
    from aizk.utilities.hashing import compute_markdown_hash

    engine = get_engine(ConversionConfig().database_url)
    aizk_uuid = uuid4()

    def _superseded_chunking_count() -> int:
        with Session(engine) as session:
            return len(
                session.exec(
                    select(PipelineRun).where(
                        PipelineRun.stage == "chunking",
                        PipelineRun.scope_key == str(aizk_uuid),
                        PipelineRun.status == RunStatus.SUPERSEDED,
                    )
                ).all()
            )

    def _active_consumes() -> str:
        with Session(engine) as session:
            active = active_chunking_run(session, str(aizk_uuid))
            return run_input(session, active.id).conversion_output_id

    # 1. The first conversion output is processed and becomes the active generation.
    with Session(engine) as session:
        old_output = _seed_conversion_output(session, text=_SUPERSEDE_OLD, label="note (rev 1)", aizk_uuid=aizk_uuid)
        session.commit()
    markdown_source.register(old_output, _SUPERSEDE_OLD, compute_markdown_hash(_SUPERSEDE_OLD))
    with Session(engine) as session:
        enqueue_output(session, old_output)
        session.commit()
    drain_worker(handler)
    log.info("1. first output %s processed -> active chunking consumes %s", old_output, _active_consumes())

    # 2. A re-conversion (newer output) supersedes the prior generation's runs.
    with Session(engine) as session:
        new_output = _seed_conversion_output(session, text=_SUPERSEDE_NEW, label="note (rev 2)", aizk_uuid=aizk_uuid)
        session.commit()
    markdown_source.register(new_output, _SUPERSEDE_NEW, compute_markdown_hash(_SUPERSEDE_NEW))
    with Session(engine) as session:
        enqueue_output(session, new_output)
        session.commit()
    drain_worker(handler)
    log.info(
        "2. re-conversion %s processed -> active consumes %s, superseded chunking runs=%d",
        new_output,
        _active_consumes(),
        _superseded_chunking_count(),
    )
    assert _active_consumes() == str(new_output)
    assert _superseded_chunking_count() >= 1, "the re-conversion should have superseded the prior generation"

    # 3. The older output runs late (re-queued): the freshness gate skips it.
    superseded_before = _superseded_chunking_count()
    with Session(engine) as session:
        old_job = session.exec(
            select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == old_output)
        ).one()
        old_job.status = WorkUnitStatus.QUEUED
        session.add(old_job)
        session.commit()
    drain_worker(handler)
    log.info(
        "3. older output %s re-run late -> active still consumes %s, superseded chunking runs=%d (unchanged)",
        old_output,
        _active_consumes(),
        _superseded_chunking_count(),
    )
    assert _active_consumes() == str(new_output), "the late older output must not supersede the newer generation"
    assert _superseded_chunking_count() == superseded_before, "the skipped older output must not supersede anything"


# %% [markdown]
# ## 12. Operator surface (list / detail / retry / cancel)
#
# The stage-owned FastAPI surface over the same temp DB. Seed two units in known
# states and drive the operator endpoints.


# %%
def demo_operator_api(log: logging.Logger) -> None:
    """Exercise list / detail / retry / cancel over seeded work-units."""
    from sqlmodel import Session

    from fastapi.testclient import TestClient

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.api.main import create_app
    from aizk.graph.datamodel import ContextualizationJob
    from aizk.pipeline.lifecycle import WorkUnitStatus

    engine = get_engine(ConversionConfig().database_url)
    with Session(engine) as session:
        failed = ContextualizationJob(
            idempotency_key=f"demo-op-failed-{uuid4().hex}",
            conversion_output_id=900001,
            aizk_uuid=uuid4(),
            status=WorkUnitStatus.FAILED,
            attempts=3,
            error_code="demo_error",
        )
        queued = ContextualizationJob(
            idempotency_key=f"demo-op-queued-{uuid4().hex}",
            conversion_output_id=900002,
            aizk_uuid=uuid4(),
            status=WorkUnitStatus.QUEUED,
        )
        session.add(failed)
        session.add(queued)
        session.commit()
        failed_id, queued_id = failed.id, queued.id

    with TestClient(create_app(), base_url="http://localhost") as client:
        listed = client.get("/v1/contextualizations").json()
        log.info("operator list: %d work-units total", listed["total"])
        detail = client.get(f"/v1/contextualizations/{failed_id}").json()
        log.info("  detail id=%s status=%s error_code=%s", detail["id"], detail["status"], detail["error_code"])
        retried = client.post(f"/v1/contextualizations/{failed_id}/retry").json()
        log.info("  retry failed unit -> status=%s", retried["status"])
        cancelled = client.post(f"/v1/contextualizations/{queued_id}/cancel").json()
        log.info("  cancel queued unit -> status=%s", cancelled["status"])


# %% [markdown]
# ## 13. At-least-once idempotency
#
# Re-running a completed work-unit (as stale recovery would) reuses its active
# runs and creates no duplicate records.


# %%
def demo_idempotent_reexecution(log: logging.Logger, handler, doc: SeededDoc) -> None:
    """Re-queue and re-run a completed unit; assert no duplicate runs/rows."""
    from sqlmodel import Session, select

    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.graph.datamodel import ContextualizationJob, ContextualizedChunk, DocumentSummary
    from aizk.pipeline.lifecycle import WorkUnitStatus
    from aizk.pipeline.run import PipelineRun

    engine = get_engine(ConversionConfig().database_url)

    def _counts() -> tuple[int, int, int]:
        with Session(engine) as session:
            return (
                len(session.exec(select(PipelineRun)).all()),
                len(session.exec(select(DocumentSummary)).all()),
                len(session.exec(select(ContextualizedChunk)).all()),
            )

    before = _counts()
    with Session(engine) as session:
        job = session.exec(
            select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == doc.conversion_output_id)
        ).one()
        job.status = WorkUnitStatus.QUEUED
        session.add(job)
        session.commit()
    drain_worker(handler)
    after = _counts()
    log.info("counts before re-run=%s after=%s (runs, summaries, variants)", before, after)
    assert before == after, "re-execution must not create duplicate runs or rows"


# %% [markdown]
# ## 14. Drive the full demo
#
# Everything above is pure definitions (side-effect-free on import). This final
# cell performs the real run; it is guarded by ``__name__ == "__main__"`` so a
# static import never triggers model or database work.

# %%
if __name__ == "__main__":
    from dotenv import load_dotenv
    import nest_asyncio

    # override=True so the .env's interpolated values win over any literal "${...}"
    # the kernel may already hold (VS Code / direnv often load .env without
    # interpolation, leaving the unexpanded substitution in os.environ).
    _ = load_dotenv(override=True)
    # The model client uses pydantic-ai's run_sync (run_until_complete); a notebook
    # kernel already owns a running loop, so allow re-entrant loop use.
    nest_asyncio.apply()

    _log = configure_demo_logging()

    _tmp_db, _tmp_url = isolate_temp_database()
    _log.info("Isolated temp database: %s", _tmp_url)

    _llm_client = build_demo_llm_client(_log)
    _documents = load_demo_documents(SAMPLE_SIZE)
    _log.info("Loaded %d document(s): %s", len(_documents), [d.label for d in _documents])

    _seeded, _markdown_source = seed_documents(_documents)
    _handler = build_handler(_markdown_source, _llm_client)

    _log.info("Enqueuing and draining the worker (real model calls per chunk)...")
    enqueue_documents(_seeded)
    drain_worker(_handler)

    _log.info("=== Work-unit lifecycle ===")
    report_lifecycle(_log, _seeded)

    _succeeded = [doc for doc in _seeded if unit_status(doc) == "succeeded"]
    if not _succeeded:
        raise RuntimeError(
            "No document processed successfully — see the error_code/error_message logged above "
            "(the model preflight passed, so this is a per-document failure, not endpoint config)."
        )
    _primary = _succeeded[0]
    _log.info("=== Chunk persistence (%s) ===", _primary.label)
    show_chunks(_log, _primary)
    _log.info("=== Document summary ===")
    show_summary(_log, _primary)
    _log.info("=== Contextualized variants + toggle ===")
    show_variants(_log, _primary)
    _log.info("=== Backward traceability ===")
    trace_backward(_log, _primary)

    _log.info("=== Run-mode independence ===")
    demo_run_mode_independence(_log, _primary)
    _log.info("=== Supersession + monotonic currentness ===")
    demo_supersession_and_currentness(_log, _handler, _markdown_source)
    _log.info("=== Operator API ===")
    demo_operator_api(_log)
    _log.info("=== At-least-once idempotency ===")
    demo_idempotent_reexecution(_log, _handler, _primary)

    _log.info("Graph contextualization demo complete.")
