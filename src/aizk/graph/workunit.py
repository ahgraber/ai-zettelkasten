"""The per-document graph unit-of-work and its two enqueue entry points.

This module is the single write path the graph stage runs for one converted
document — ``split → persist_chunks → summarize → contextualize`` — plus the two
enqueue patterns that feed it. Chunk persistence and contextualization are **one
required pipeline**, not separately-gated steps: :func:`process_document` always
runs the whole path, and the contextualization toggle is an enqueue/eval lever
(which documents to enqueue, and the downstream raw-vs-contextualized comparison
via :func:`aizk.graph.contextualization.resolve_chunk_text`), never a per-unit
branch.

Both run modes are **enqueue patterns over one write path**, so the produced run
records are identical regardless of mode:

- :func:`enqueue_document` — incremental: enqueue one work-unit when a document
  is ingested.
- :func:`enqueue_backfill` — bulk: enqueue work-units for many documents
  (throttling and batching are the caller's scheduling concern).

Both dedupe on ``idempotency_key``, so re-enqueueing the same document reuses the
open work-unit rather than creating a second.

:func:`process_document` resolves its inputs by **locator** (``conversion_output_id``
→ Markdown, via the injected :class:`MarkdownSource`) but derives every run's
reuse/supersession by **content** — the durable ``aizk_uuid``, the markdown hash,
the version stamps, the prompt/model derivation keys, and the ordered
``chunk_id``s — never from ``id``, ``conversion_output_id``, or any other local
row handle. The Markdown source is injected so the graph stage stays decoupled
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
import datetime
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sqlmodel import Session, select

from aizk.chunking import SPLITTER_VERSION, split
from aizk.graph.contextualization import (
    consumed_output_memo_keys,
    contextualize_chunks,
    resolve_revisions,
    resolve_summary_text,
    summarize_document,
)
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.db import begin_immediate
from aizk.graph.persistence import memo_delete_keys, persist_chunks
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.utilities.hashing import compute_markdown_hash

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from uuid import UUID

    from sqlalchemy import Engine

    from aizk.graph.llm import LLMClient

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class LoadedMarkdown:
    """The Markdown a :class:`MarkdownSource` resolved for a conversion output.

    ``markdown_hash_xx64`` is the hash the conversion stage recorded for this
    artifact; :func:`process_document` verifies the fetched ``text`` hashes to it
    before splitting, so blob/metadata drift fails closed rather than silently
    chunking the wrong content.
    """

    text: str
    markdown_hash_xx64: str


@runtime_checkable
class MarkdownSource(Protocol):
    """The injected seam that fetches a converted document's Markdown by locator.

    Keeps the graph stage decoupled from the conversion stage's blob storage: the
    production implementation reads the ``ConversionOutput`` row and downloads the
    Markdown blob, while tests supply a deterministic in-memory double. The stage
    fetches Markdown only through this one method.
    """

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        """Return the Markdown (and its recorded hash) for a conversion output locator."""
        ...


@runtime_checkable
class OutputFreshness(Protocol):
    """The injected seam deciding whether a conversion output is still the source's latest.

    Production resolves this from the conversion outputs (the highest output id for
    the ``aizk_uuid`` wins under the single serialized writer); tests supply a
    deterministic double. Queried inside the persist transaction so the
    freshness check and the supersede are atomic.
    """

    def is_current(self, session: "Session", aizk_uuid: "UUID", conversion_output_id: int) -> bool:
        """Return ``True`` iff ``conversion_output_id`` is the latest output for the source."""
        ...


@dataclass(frozen=True)
class ProcessResult:
    """The run records :func:`process_document` produced (or reused) for a document.

    Carries the run ids and counts as primitives (not ORM rows, which detach once
    the persist transaction commits) so the caller can finalize, log, or assert
    without re-querying. ``chunking_run_id`` is the root of the backward-trace
    chain; ``summary_run_id`` links the summary; ``variant_count`` is one per
    persisted chunk.
    """

    chunking_run_id: int
    summary_run_id: int
    chunk_count: int
    variant_count: int


@dataclass(frozen=True)
class SkippedSuperseded:
    """Returned when the unit did nothing because a newer source output already won.

    The freshness gate found a newer ``ConversionOutput`` for the source, so the
    older unit wrote nothing rather than superseding the newer generation's runs.
    """

    conversion_output_id: int


@dataclass(frozen=True)
class Cancelled:
    """Returned when cancellation was observed, so the unit wrote nothing.

    The injected ``is_cancelled`` check fired (a runner timeout/cancel), so
    generation and/or the persist write were skipped. The runner resolves the
    terminal ``CANCELLED`` / ``TIMED_OUT`` status from its own slot state.
    """


def process_document(
    engine: "Engine",
    client: "LLMClient",
    *,
    aizk_uuid: "UUID",
    conversion_output_id: int,
    markdown_source: MarkdownSource,
    freshness: OutputFreshness,
    is_cancelled: "Callable[[], bool] | None" = None,
) -> ProcessResult | SkippedSuperseded | Cancelled:
    """Run the full graph write path for one converted document.

    A read-only **preflight** runs first: if the output no longer belongs to the
    source or a newer one has superseded it, return :class:`SkippedSuperseded`
    before any fetch or model call, so foreign/stale content never reaches the
    model. Then two phases. **Plan (no write lock):** fetch the Markdown via the
    ``conversion_output_id`` locator, verify its hash, split it, and run the
    summary + per-chunk revision LLM passes. **Apply (one short ``BEGIN IMMEDIATE``):**
    the same freshness gate again — authoritatively and atomically with the
    supersede (the preflight is best-effort) — then persist the chunking run +
    chunk rows, the summary, and the variants, scoped to ``str(aizk_uuid)`` and
    keyed by content. The chunks are contextualized in document order (the
    splitter's order), not the ``chunk_id``-ordered manifest.

    Cooperative cancellation: ``is_cancelled`` is checked before the (expensive)
    LLM generation and again at the start of the persist transaction, so a
    runner-driven cancel/timeout skips the model calls and/or the domain write
    rather than committing work for a unit that is being cancelled.

    Idempotent on the runs' derivation keys: re-executing an unchanged document
    reuses its active runs and produces no duplicate records (including the
    zero-chunk case). Owns its own write transaction; the caller does not wrap it.

    Args:
        engine: The shared engine; the apply phase opens its own ``BEGIN IMMEDIATE``.
        client: The single model access point for the summary and variant passes.
        aizk_uuid: The durable source identity (runs scope to ``str(aizk_uuid)``).
        conversion_output_id: The conversion artifact locator to process.
        markdown_source: The injected seam that fetches the document's Markdown.
        freshness: The injected seam deciding whether this output is still current.
        is_cancelled: Optional cooperative-cancellation probe; when it returns
            ``True`` the unit stops without writing and returns :class:`Cancelled`.

    Returns:
        A :class:`ProcessResult` with the run ids and counts, :class:`SkippedSuperseded`
        when a newer output has already won, or :class:`Cancelled` when cancelled.

    Raises:
        ValueError: If the fetched Markdown does not hash to the recorded
            ``markdown_hash_xx64`` (blob/metadata drift) — guarding against
            chunking content that disagrees with the conversion stage's record.
    """
    scope_key = str(aizk_uuid)
    locator = str(conversion_output_id)

    def _cancelled() -> bool:
        return is_cancelled is not None and is_cancelled()

    # --- Early ownership/currentness preflight (read-only, best-effort). ---
    # Skip the S3 fetch and the LLM passes entirely for an output that no longer
    # belongs to the source or has already been superseded by a newer conversion, so
    # foreign or stale content never reaches the model and no fetch/generation work
    # is wasted. Best-effort: the authoritative, atomic gate runs again inside the
    # persist transaction below (a newer output may still land in between).
    with Session(engine) as preflight_session:
        if not freshness.is_current(preflight_session, aizk_uuid, conversion_output_id):
            logger.info(
                "Skipping superseded/foreign conversion output %s for source %s before fetch (preflight)",
                conversion_output_id,
                scope_key,
            )
            return SkippedSuperseded(conversion_output_id=conversion_output_id)

    # --- Plan phase: S3 fetch + LLM, with no write lock held. ---
    loaded = markdown_source.load(conversion_output_id)
    actual_hash = compute_markdown_hash(loaded.text)
    if actual_hash != loaded.markdown_hash_xx64:
        raise ValueError(
            f"fetched Markdown for conversion_output_id={conversion_output_id} hashes to "
            f"{actual_hash!r}, not the recorded {loaded.markdown_hash_xx64!r}"
        )
    chunks = split(
        loaded.text,
        doc_id=scope_key,
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
        aizk_uuid=scope_key,
        markdown_hash_xx64=loaded.markdown_hash_xx64,
        document_text=loaded.text,
    )
    # ``None`` signals the generation phase resolved to reuse a complete active
    # variant run (no revisions to carry); ``reuse_only`` then makes the persist
    # phase fail retryably if that run vanished between plan and apply rather than
    # misinterpreting an empty set as a torn run.
    revisions = resolve_revisions(
        engine,
        client,
        aizk_uuid=scope_key,
        summary_text=summary_text,
        markdown_hash_xx64=loaded.markdown_hash_xx64,
        ordered_chunks=chunks,
        splitter_version=SPLITTER_VERSION,
    )

    # --- Apply phase: one short write transaction. ---
    with begin_immediate(engine) as session:
        # Do not commit domain writes for a unit that is being cancelled.
        if _cancelled():
            logger.info("Cancellation observed before persist for conversion output %s", conversion_output_id)
            return Cancelled()
        if not freshness.is_current(session, aizk_uuid, conversion_output_id):
            logger.info(
                "Skipping superseded conversion output %s for source %s (a newer output won)",
                conversion_output_id,
                scope_key,
            )
            return SkippedSuperseded(conversion_output_id=conversion_output_id)

        chunking_run = persist_chunks(
            session,
            aizk_uuid=scope_key,
            conversion_output_id=locator,
            markdown_hash_xx64=loaded.markdown_hash_xx64,
            splitter_version=SPLITTER_VERSION,
            chunks=chunks,
        )
        summary = summarize_document(
            session,
            client,
            aizk_uuid=scope_key,
            conversion_output_id=locator,
            markdown_hash_xx64=loaded.markdown_hash_xx64,
            document_text=loaded.text,
            precomputed_summary_text=summary_text,
        )
        variants = contextualize_chunks(
            session,
            client,
            aizk_uuid=scope_key,
            summary=summary,
            chunks=chunks,
            chunking_run_id=chunking_run.id,
            splitter_version=SPLITTER_VERSION,
            precomputed_revisions=revisions,
            reuse_only=revisions is None,
        )
        # Prune the memo entries this generation consumed, atomically with the
        # persist: the summary and revisions now live in DocumentSummary /
        # ContextualizedChunk, so the checkpoint rows are redundant. Key-exact, so a
        # concurrent same-source attempt under different keys keeps its checkpoints.
        memo_delete_keys(
            session,
            scope_key,
            consumed_output_memo_keys(
                summary_text,
                markdown_hash_xx64=loaded.markdown_hash_xx64,
                ordered_chunks=chunks,
                splitter_version=SPLITTER_VERSION,
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
        scope_key,
        result.chunk_count,
        result.variant_count,
    )
    return result


def _idempotency_key(conversion_output_id: int) -> str:
    """Return the enqueue-dedupe key for a conversion output.

    Keyed by the artifact locator: re-enqueueing the same conversion output reuses
    its work-unit, while a re-conversion (a new ``conversion_output_id``) is a
    distinct unit that splits its own Markdown.
    """
    return f"conversion_output:{conversion_output_id}"


def enqueue_document(
    session: "Session",
    *,
    conversion_output_id: int,
    aizk_uuid: "UUID",
) -> ContextualizationJob:
    """Enqueue one document's work-unit (incremental mode), deduped on ``idempotency_key``.

    If a work-unit for this conversion output already exists, it is returned
    unchanged rather than duplicated — so an incremental re-ingest, or a backfill
    overlapping an already-queued unit, reuses the open unit. Otherwise a new
    ``QUEUED`` unit is inserted and flushed (so its ``id`` is available).

    Does **not** commit; the caller owns the surrounding transaction.

    Args:
        session: Active session; the caller owns commit/rollback.
        conversion_output_id: The conversion artifact locator to process.
        aizk_uuid: The durable source identity, resolved by the caller from the
            conversion output and carried onto the unit's runs and events.

    Returns:
        The existing or newly-created :class:`ContextualizationJob`.
    """
    key = _idempotency_key(conversion_output_id)
    existing = session.exec(
        select(ContextualizationJob).where(ContextualizationJob.idempotency_key == key)
    ).one_or_none()
    if existing is not None:
        logger.debug(
            "Reusing contextualization work-unit id=%s for conversion_output_id=%s",
            existing.id,
            conversion_output_id,
        )
        return existing

    job = ContextualizationJob(
        idempotency_key=key,
        conversion_output_id=conversion_output_id,
        aizk_uuid=aizk_uuid,
        status=WorkUnitStatus.QUEUED,
        queued_at=_utcnow(),
    )
    session.add(job)
    session.flush()
    logger.debug(
        "Enqueued contextualization work-unit id=%s for conversion_output_id=%s", job.id, conversion_output_id
    )
    return job


def enqueue_backfill(
    session: "Session",
    documents: "Iterable[tuple[int, UUID]]",
) -> list[ContextualizationJob]:
    """Enqueue work-units for many documents (bulk/backfill mode) through the single path.

    Each ``(conversion_output_id, aizk_uuid)`` pair is enqueued via
    :func:`enqueue_document`, so the same dedupe applies and the resulting units
    are identical to incremental enqueue — only volume and scheduling differ.
    Throttling and per-document commit batching are the caller's concern; this
    function only stages the rows and does not commit.

    Returns:
        The enqueued (or reused) work-units, one per input document.
    """
    return [
        enqueue_document(session, conversion_output_id=conversion_output_id, aizk_uuid=aizk_uuid)
        for conversion_output_id, aizk_uuid in documents
    ]
