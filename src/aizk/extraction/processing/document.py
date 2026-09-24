"""Extract and persist one source's mentions under an active extraction run.

Read and extraction work occur before the short write transaction.
Unchanged derivation keys reuse the active run; changed inputs create a
superseding run. The extraction and co-occurrence rows commit together.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlmodel import Session, func, select

from aizk.db.engine import begin_immediate
from aizk.extraction.core.applicability import record_extraction_run_applicability
from aizk.extraction.core.currentness import _resolve_upstream_run, _upstream_output_fingerprint
from aizk.extraction.core.extractors import (
    MATERIALIZER_VERSION,
    EntityExtractor,
    ExtractionInput,
    extract_chunk_mentions,
    select_extraction_input,
)
from aizk.graph.chunks import active_chunking_run, document_order_chunks
from aizk.graph.datamodel import INPUT_KIND_RAW, ExtractionRunInput, Mention
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.graph.mentions import (
    INPUT_POLICY_RAW,
    InputPolicy,
    MentionDraft,
    ResolvedExtractionConfig,
    active_extraction_run,
    extraction_derivation_key,
    open_extraction_run,
    persist_mentions,
)
from aizk.graph.variants import StalePlanError
from aizk.pipeline.identity import content_fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine

    from aizk.graph.datamodel import Chunk
    from aizk.pipeline.run import PipelineRun

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentExtractionResult:
    """Result of extracting one source.

    Carries primitive run and output counts that remain usable after the write
    transaction closes.
    """

    run_id: int
    source_id: UUID
    chunk_count: int
    mention_count: int


@dataclass(frozen=True)
class Cancelled:
    """Result when cancellation caused the unit to write nothing."""


@dataclass(frozen=True)
class _ChunkWorkItem:
    """One chunk's extraction inputs, materialized into plain values inside the read session.

    The extract phase runs after the read session closes, so it must never
    touch ORM instances (whose attribute access can require a live session);
    this snapshot carries exactly the per-chunk facts
    :func:`~aizk.extraction.core.extractors.extract_chunk_mentions` consumes.
    """

    chunk_id: UUID
    raw_text: str
    extraction_input: ExtractionInput
    #: The chunk's portable content key (:func:`~aizk.graph.fingerprints.chunk_key`) lets
    #: fingerprint construction run after the read session closes.
    chunk_key: str


def _replan_upstream(session: "Session", *, source_id: UUID, input_policy: InputPolicy) -> PipelineRun:
    """Resolve the upstream run inside the write transaction.

    Raises:
        StalePlanError: If the source has no active chunking run.
    """
    chunking_run = active_chunking_run(session, source_id)
    if chunking_run is None or chunking_run.id is None:
        raise StalePlanError(
            f"source {source_id!r} has no active chunking run at persist; "
            "retry to re-resolve and re-extract against current state"
        )
    return _resolve_upstream_run(session, source_id=source_id, input_policy=input_policy, chunking_run=chunking_run)


def _resolve_extraction_input(session: "Session", chunk: "Chunk", *, input_policy: InputPolicy) -> ExtractionInput:
    """Resolve a chunk's extraction input under ``input_policy``.

    Raw policy returns chunk text. Contextualized policy uses the active variant
    with raw fallback.
    """
    if input_policy == INPUT_POLICY_RAW:
        return ExtractionInput(text=chunk.text, input_kind=INPUT_KIND_RAW)
    return select_extraction_input(session, chunk)


def _reconcile_active_run(
    engine: "Engine",
    *,
    source_id: UUID,
    input_policy: InputPolicy,
    resolved: "ResolvedExtractionConfig",
    planned_run_id: int,
    planned_upstream_run_id: int,
    planned_input_fingerprint: str,
    chunk_count: int,
) -> DocumentExtractionResult:
    """Reconcile an active run whose derivation key still matches.

    Revalidate the planned run and upstream fingerprint in the write transaction.
    Record applicability when the upstream run changed without changing the
    consumed payload. Never rewrite the run-input row.
    """
    with begin_immediate(engine) as session:
        current_upstream = _replan_upstream(session, source_id=source_id, input_policy=input_policy)
        current_run = active_extraction_run(session, source_id)
        if (
            current_run is None
            or current_run.id != planned_run_id
            or current_upstream.id != planned_upstream_run_id
            or _upstream_output_fingerprint(session, current_upstream) != planned_input_fingerprint
        ):
            raise StalePlanError(
                f"extraction run for source {source_id!r} planned for reuse changed before persist; "
                "retry to re-resolve and re-extract against current state"
            )
        assert current_run.id is not None  # noqa: S101 — an active run is always persisted
        run_input = session.get(ExtractionRunInput, current_run.id)
        assert run_input is not None  # noqa: S101 — its persist is atomic with the run
        if run_input.upstream_run_id != current_upstream.id:
            record_extraction_run_applicability(
                session,
                run_id=current_run.id,
                upstream_run_id=current_upstream.id,
                input_policy=input_policy,
                resolved=resolved,
            )
        mention_count = session.exec(
            select(func.count()).select_from(Mention).where(Mention.run_id == current_run.id)
        ).one()
        result = DocumentExtractionResult(
            run_id=current_run.id,
            source_id=source_id,
            chunk_count=chunk_count,
            mention_count=mention_count,
        )
    logger.info(
        "Reusing extraction run for source: candidate key matches the active run",
        extra={"run_id": result.run_id, "source_id": str(source_id)},
    )
    return result


def extract_document(
    engine: "Engine",
    *,
    source_id: UUID,
    extractor: EntityExtractor,
    input_policy: InputPolicy,
    is_cancelled: "Callable[[], bool] | None" = None,
) -> DocumentExtractionResult | Cancelled:
    """Extract and persist one source in one document transaction.

    The function calls the extractor at most once per chunk. It reuses the active
    run and records applicability when the resolved payload and configuration
    match. Otherwise it opens a superseding run and persists the run input,
    mentions, and co-occurrences.

    The read and extraction phases run outside the write transaction. Model calls
    do not hold the writer lock. A stale plan raises without committing.
    Cancellation before extraction or persistence returns :class:`Cancelled`.

    Args:
        engine: Shared engine for the read and write sessions.
        source_id: Source identity for the extraction run.
        extractor: Injected NER extractor.
        input_policy: Raw or contextualized input policy.
        is_cancelled: Probe that reports whether cancellation was requested.

    Returns:
        The active run ID and output counts.

    Raises:
        ValueError: If the input policy is invalid or the source has no active
            chunking run.
        StalePlanError: If planned upstream state changes before persistence.
    """

    def _cancelled() -> bool:
        return is_cancelled is not None and is_cancelled()

    resolved = ResolvedExtractionConfig(
        extractor_version=extractor.extractor_version,
        materializer_version=MATERIALIZER_VERSION,
        input_policy=input_policy,
    )

    with Session(engine) as read_session:
        chunking_run = active_chunking_run(read_session, source_id)
        if chunking_run is None or chunking_run.id is None:
            raise ValueError(
                f"source {source_id!r} has no active chunking run; an unchunked source has nothing to extract"
            )
        # Materialize each chunk's work item into plain values while the read
        # session is still open, so code after the session closes never touches ORM
        # instances detached from a closed session.
        work_items = [
            _ChunkWorkItem(
                chunk_id=chunk.chunk_id,
                raw_text=chunk.text,
                extraction_input=_resolve_extraction_input(read_session, chunk, input_policy=input_policy),
                chunk_key=chunk_key(chunk),
            )
            for chunk, _, _ in document_order_chunks(read_session, chunking_run.id)
        ]
        upstream_run = _resolve_upstream_run(
            read_session, source_id=source_id, input_policy=input_policy, chunking_run=chunking_run
        )
        assert upstream_run.id is not None  # noqa: S101 — an active run is always persisted
        active_run = active_extraction_run(read_session, source_id)

    # The payload extraction reads, in document order — equal by construction to
    # the consumed upstream run's own persisted output fingerprint.
    planned_input_fingerprint = payload_fingerprint(
        [
            PayloadItem(
                item.chunk_key, item.extraction_input.input_kind, content_fingerprint(item.extraction_input.text)
            )
            for item in work_items
        ]
    )
    candidate_key = extraction_derivation_key(
        input_fingerprint=planned_input_fingerprint, config_fingerprint=resolved.config_fingerprint()
    )

    # Skip the expensive model passes if cancellation was already requested.
    if _cancelled():
        logger.info("Cancellation observed before extraction for source %s", source_id)
        return Cancelled()
    if active_run is not None and active_run.derivation_key == candidate_key:
        assert active_run.id is not None  # noqa: S101 — an active run is always persisted
        return _reconcile_active_run(
            engine,
            source_id=source_id,
            input_policy=input_policy,
            resolved=resolved,
            planned_run_id=active_run.id,
            planned_upstream_run_id=upstream_run.id,
            planned_input_fingerprint=planned_input_fingerprint,
            chunk_count=len(work_items),
        )

    drafts: list[MentionDraft] = []
    for item in work_items:
        drafts.extend(
            extract_chunk_mentions(
                chunk_id=item.chunk_id,
                raw_chunk_text=item.raw_text,
                extraction_input=item.extraction_input,
                extractor=extractor,
            )
        )

    with begin_immediate(engine) as session:
        # Do not commit domain writes for a unit that is being cancelled.
        if _cancelled():
            logger.info("Cancellation observed before persist for source %s", source_id)
            return Cancelled()
        current_upstream = _replan_upstream(session, source_id=source_id, input_policy=input_policy)
        if (
            current_upstream.id != upstream_run.id
            or _upstream_output_fingerprint(session, current_upstream) != planned_input_fingerprint
        ):
            raise StalePlanError(
                f"upstream state for source {source_id!r} changed before persist; "
                "retry to re-resolve and re-extract against current state"
            )
        run = open_extraction_run(
            session, source_id=source_id, resolved=resolved, input_fingerprint=planned_input_fingerprint
        )
        assert run.id is not None  # noqa: S101 — a persisted run always carries an id
        # One immutable ExtractionRunInput row per run: a retry that reuses the
        # active run must not insert a second row under the same run_id.
        if session.get(ExtractionRunInput, run.id) is None:
            session.add(
                ExtractionRunInput(
                    run_id=run.id,
                    upstream_run_id=current_upstream.id,
                    input_policy=input_policy,
                    input_fingerprint=planned_input_fingerprint,
                    config_fingerprint=resolved.config_fingerprint(),
                )
            )
        persisted = persist_mentions(session, run=run, mentions=drafts)
        result = DocumentExtractionResult(
            run_id=run.id,
            source_id=source_id,
            chunk_count=len(work_items),
            mention_count=len(persisted),
        )

    logger.debug(
        "Extracted source=%s run id=%s: %d chunks, %d mentions",
        source_id,
        result.run_id,
        result.chunk_count,
        result.mention_count,
    )
    return result


def extract_source(
    engine: "Engine",
    *,
    source_id: UUID,
    extractor: EntityExtractor,
    input_policy: InputPolicy,
) -> DocumentExtractionResult:
    """Extract one source using the per-document extraction operation."""
    return extract_document(engine, source_id=source_id, extractor=extractor, input_policy=input_policy)
