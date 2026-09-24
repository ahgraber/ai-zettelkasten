"""The per-document graph unit-of-work.

This module is the single write path the graph stage runs for one converted
document — ``split → persist_chunks → summarize → contextualize``. Chunk persistence and contextualization are **one
required pipeline**, not separately-gated steps: :func:`process_document` always
runs the whole path, and the contextualization toggle is an enqueue/eval lever
(which documents to enqueue, and the downstream raw-vs-contextualized comparison
via :func:`aizk.graph.variants.resolve_chunk_text`), never a per-unit
branch.

:func:`process_document` resolves its inputs by **locator** (``conversion_output_id``
→ Markdown, via the injected :class:`MarkdownSource`) but derives every run's
reuse/supersession by **content** — the durable ``source_id``, the markdown hash,
the version stamps, the prompt/model derivation keys, and the ordered chunk
content keys — never from ``id``, ``conversion_output_id``, the surrogate
``chunk_id``, or any other local row handle. The Markdown source is injected so the graph stage stays decoupled
from the conversion stage's blob storage and is deterministically testable.

**Two phases, one short write lock.** The Markdown fetch (S3) and the LLM passes
(summary + per-chunk revisions) run with **no write transaction held**; only the
persistence runs inside a single short ``BEGIN IMMEDIATE``, so model/IO latency
never blocks other writers (conversion, enqueue, retry/cancel) on the single
serialized SQLite writer.

**Monotonic currentness.** A read-only **preflight** checks the injected
:class:`OutputFreshness` before the fetch/generate work, and the same gate runs
again — authoritatively and atomically with the supersede — inside the write
transaction: if a newer ``ConversionOutput`` already exists for the source (or
the output belongs to a different source), the older unit writes nothing and
returns :class:`SkippedSuperseded`. An older work-unit can never supersede a
newer conversion's runs back to stale text, even if it runs late, and
foreign/superseded content never reaches the model.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sqlmodel import Session

from aizk.chunking import SPLITTER_VERSION, split
from aizk.contextualization.core.keys import consumed_output_memo_keys
from aizk.contextualization.core.revisions import contextualize_chunks, resolve_revisions
from aizk.contextualization.core.summary import resolve_summary_text, summarize_document
from aizk.db.engine import begin_immediate
from aizk.graph.chunks import memo_delete_keys, persist_chunks
from aizk.pipeline.identity import content_fingerprint
from aizk.utilities.hashing import compute_markdown_hash

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from sqlalchemy import Engine

    from aizk.ai.providers.llm import LLMClient, ModelConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedMarkdown:
    """Markdown text and its recorded hash for a conversion output."""

    text: str
    markdown_hash_xx64: str


@runtime_checkable
class MarkdownSource(Protocol):
    """Loads converted Markdown and its recorded hash by locator."""

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        """Return the Markdown (and its recorded hash) for a conversion output locator."""
        ...


@runtime_checkable
class OutputFreshness(Protocol):
    """Checks whether a conversion output is the latest output for a source.

    The caller supplies the persistence session so the check can share a
    transaction with supersession.
    """

    def is_current(self, session: "Session", source_id: "UUID", conversion_output_id: int) -> bool:
        """Return ``True`` iff ``conversion_output_id`` is the latest output for the source."""
        ...


@dataclass(frozen=True)
class ProcessResult:
    """Run identifiers and counts produced or reused by :func:`process_document`.

    The fields are primitives and remain usable after the persistence transaction.
    ``chunking_run_id`` roots lineage; ``variant_count`` counts one variant per
    persisted chunk.
    """

    chunking_run_id: int
    summary_run_id: int
    chunk_count: int
    variant_count: int


@dataclass(frozen=True)
class SkippedSuperseded:
    """Result when a newer source output superseded this work unit.

    The unit wrote nothing; ``conversion_output_id`` identifies the skipped output.
    """

    conversion_output_id: int


@dataclass(frozen=True)
class Cancelled:
    """Result when cancellation caused the unit to write nothing."""


def process_document(
    engine: "Engine",
    client: "LLMClient",
    *,
    source_id: "UUID",
    conversion_output_id: int,
    markdown_source: MarkdownSource,
    freshness: OutputFreshness,
    model_config: "ModelConfig",
    is_cancelled: "Callable[[], bool] | None" = None,
) -> ProcessResult | SkippedSuperseded | Cancelled:
    """Process one converted document through chunking and contextualization.

    The function checks output ownership, fetches and verifies Markdown, and runs
    model generation without a write lock. It rechecks freshness and persists the
    chunking, summary, and variant runs in one transaction, in document order.
    Cancellation before generation or persistence returns :class:`Cancelled`; a
    stale output returns :class:`SkippedSuperseded`.

    Unchanged derivation keys reuse active runs. The caller must not wrap this
    function in a write transaction.

    Raises:
        ValueError: If fetched Markdown does not match its recorded hash.
    """
    locator = str(conversion_output_id)

    def _cancelled() -> bool:
        return is_cancelled is not None and is_cancelled()

    # Preflight ownership/currentness before fetching; repeat the authoritative check in the write transaction.
    # Skip the S3 fetch and the LLM passes entirely for an output that no longer
    # belongs to the source or has already been superseded by a newer conversion, so
    # foreign or stale content never reaches the model and no fetch/generation work
    # is wasted. Best-effort: the authoritative, atomic gate runs again inside the
    # persist transaction below (a newer output may still land in between).
    with Session(engine) as preflight_session:
        if not freshness.is_current(preflight_session, source_id, conversion_output_id):
            logger.info(
                "Skipping superseded/foreign conversion output %s for source %s before fetch (preflight)",
                conversion_output_id,
                source_id,
            )
            return SkippedSuperseded(conversion_output_id=conversion_output_id)

    # Fetch Markdown and call the LLM without holding the write lock.
    loaded = markdown_source.load(conversion_output_id)
    actual_hash = compute_markdown_hash(loaded.text)
    if actual_hash != loaded.markdown_hash_xx64:
        raise ValueError(
            f"fetched Markdown for conversion_output_id={conversion_output_id} hashes to "
            f"{actual_hash!r}, not the recorded {loaded.markdown_hash_xx64!r}"
        )
    chunks = split(
        loaded.text,
        source_id=str(source_id),
        converted_artifact_id=locator,
        markdown_hash_xx64=loaded.markdown_hash_xx64,
    )
    # Skip the expensive model passes if cancellation was already requested.
    if _cancelled():
        logger.info("Cancellation observed before generation for conversion output %s", conversion_output_id)
        return Cancelled()
    # Resolve the summary text and per-chunk revisions, reusing validated model
    # output retained by a prior partial attempt (and the active runs) so a retry
    # re-invokes the model only for outputs not yet produced. These resolvers
    # perform their own short autonomous memo commits and never hold the write lock
    # across a model call; the revisions are conditioned on the resolved summary, so
    # the variants record the same summary the revision was actually built from.
    summary_text = resolve_summary_text(
        engine,
        client,
        source_id=source_id,
        document_text=loaded.text,
        model_config=model_config,
    )
    # ``None`` means a complete active variant run can be reused without revisions.
    # ``reuse_only`` makes a vanished run fail retryably instead of treating an empty
    # precomputed set as a torn run.
    revisions = resolve_revisions(
        engine,
        client,
        source_id=source_id,
        summary_text=summary_text,
        ordered_chunks=chunks,
        splitter_version=SPLITTER_VERSION,
        model_config=model_config,
    )

    # Persist the result in one short write transaction.
    with begin_immediate(engine) as session:
        # Do not commit domain writes for a unit that is being cancelled.
        if _cancelled():
            logger.info("Cancellation observed before persist for conversion output %s", conversion_output_id)
            return Cancelled()
        if not freshness.is_current(session, source_id, conversion_output_id):
            logger.info(
                "Skipping superseded conversion output %s for source %s (a newer output won)",
                conversion_output_id,
                source_id,
            )
            return SkippedSuperseded(conversion_output_id=conversion_output_id)

        chunking_run, persisted_chunks = persist_chunks(
            session,
            source_id=source_id,
            conversion_output_id=locator,
            markdown_hash_xx64=loaded.markdown_hash_xx64,
            markdown_fingerprint=content_fingerprint(loaded.text),
            splitter_version=SPLITTER_VERSION,
            chunks=chunks,
        )
        summary = summarize_document(
            session,
            client,
            source_id=source_id,
            conversion_output_id=locator,
            markdown_hash_xx64=loaded.markdown_hash_xx64,
            document_text=loaded.text,
            model_config=model_config,
            precomputed_summary_text=summary_text,
        )
        variants = contextualize_chunks(
            session,
            client,
            source_id=source_id,
            summary=summary,
            chunks=persisted_chunks,
            chunking_run_id=chunking_run.id,
            splitter_version=SPLITTER_VERSION,
            model_config=model_config,
            precomputed_revisions=revisions,
            reuse_only=revisions is None,
        )
        # Prune the memo entries this generation consumed, atomically with the
        # persist: the summary and revisions now live in DocumentSummary /
        # ContextualizedChunk, so the checkpoint rows are redundant. Key-exact, so a
        # concurrent same-source attempt under different keys keeps its checkpoints.
        memo_delete_keys(
            session,
            source_id,
            consumed_output_memo_keys(
                loaded.text,
                summary_text,
                ordered_chunks=chunks,
                splitter_version=SPLITTER_VERSION,
                model_config=model_config,
            ),
        )
        # Capture primitives while the rows are still attached (commit expires them).
        result = ProcessResult(
            chunking_run_id=chunking_run.id,
            summary_run_id=summary.run_id,
            chunk_count=len(chunks),
            variant_count=len(variants),
        )
    logger.debug(
        "Processed document conversion_output_id=%s source=%s: %d chunks, %d variants",
        conversion_output_id,
        source_id,
        result.chunk_count,
        result.variant_count,
    )
    return result
