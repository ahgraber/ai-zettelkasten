"""Extract and persist one source's mentions under an active extraction run.

The module provides currentness classification and incremental and bulk entry
points. Read and extraction work occur before the short write transaction.
Unchanged derivation keys reuse the active run; changed inputs create a
superseding run. The extraction and co-occurrence rows commit together.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlmodel import Session, func, select

from aizk.graph.contextualization import VARIANT_STAGE, StalePlanError
from aizk.graph.datamodel import (
    INPUT_KIND_RAW,
    ChunkRunInput,
    ContextualizationRunInput,
    ExtractionRunApplicability,
    ExtractionRunInput,
    Mention,
)
from aizk.graph.db import begin_immediate
from aizk.graph.extraction import (
    MATERIALIZER_VERSION,
    EntityExtractor,
    ExtractionInput,
    extract_chunk_mentions,
    select_extraction_input,
)
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.graph.mention_store import (
    INPUT_POLICY_CONTEXTUALIZED,
    INPUT_POLICY_RAW,
    MENTION_EXTRACTION_STAGE,
    InputPolicy,
    MentionDraft,
    ResolvedExtractionConfig,
    active_extraction_run,
    extraction_derivation_key,
    open_extraction_run,
    persist_mentions,
)
from aizk.graph.persistence import CHUNKING_STAGE, active_chunking_run, document_order_chunks
from aizk.pipeline.identity import content_fingerprint
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

    from aizk.graph.datamodel import Chunk

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
class _ChunkWorkItem:
    """One chunk's extraction inputs, materialized into plain values inside the read session.

    The extract phase runs after the read session closes, so it must never
    touch ORM instances (whose attribute access can require a live session);
    this snapshot carries exactly the per-chunk facts
    :func:`~aizk.graph.extraction.extract_chunk_mentions` consumes.
    """

    chunk_id: UUID
    raw_text: str
    extraction_input: ExtractionInput
    #: The chunk's portable content key (:func:`~aizk.graph.fingerprints.chunk_key`) lets
    #: fingerprint construction run after the read session closes.
    chunk_key: str


class Currentness(str, Enum):
    """Freshness of a source's active extraction run.

    ``CURRENT`` matches resolved provenance. ``NEEDS_RECONCILIATION`` has the same
    payload but lacks applicability for the resolved upstream run. ``STALE`` has a
    changed payload or configuration.
    """

    CURRENT = "current"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    STALE = "stale"


def _resolve_upstream_run(
    session: "Session",
    *,
    source_id: UUID,
    input_policy: InputPolicy,
    chunking_run: PipelineRun,
) -> PipelineRun:
    """Resolve the upstream run used by extraction.

    Raw input always uses the active chunking run. Contextualized input uses the
    active variant run when present, and otherwise uses the chunking run.
    """
    if input_policy == INPUT_POLICY_CONTEXTUALIZED:
        variant_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == VARIANT_STAGE,
                PipelineRun.scope_id == source_id,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one_or_none()
        if variant_run is not None:
            return variant_run
    return chunking_run


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


def _upstream_output_fingerprint(session: "Session", upstream_run: "PipelineRun") -> str:
    """Return the persisted output fingerprint for a chunking or variant run.

    The function reads the producing stage's provenance row instead of
    recomputing the fingerprint.

    Raises:
        ValueError: If the stage is unsupported or its provenance row is missing.
    """
    assert upstream_run.id is not None  # noqa: S101 — an active run is always persisted
    if upstream_run.stage == CHUNKING_STAGE:
        chunk_input = session.get(ChunkRunInput, upstream_run.id)
        if chunk_input is None:
            raise ValueError(f"chunking run {upstream_run.id!r} has no recorded ChunkRunInput")
        return chunk_input.chunk_set_fingerprint
    if upstream_run.stage == VARIANT_STAGE:
        context_input = session.get(ContextualizationRunInput, upstream_run.id)
        if context_input is None:
            raise ValueError(f"contextualization run {upstream_run.id!r} has no recorded ContextualizationRunInput")
        return context_input.output_fingerprint
    raise ValueError(
        f"run {upstream_run.id!r} has stage {upstream_run.stage!r}, expected a chunking or contextualization run"
    )


def _classify_extraction(
    session: "Session", *, resolved: "ResolvedExtractionConfig", scope_ids: "Sequence[UUID] | None" = None
) -> dict[UUID, "Currentness"]:
    """Classify active extraction runs against resolved upstream and configuration.

    The query count is fixed for the selected scope, and the function does not
    load chunk or variant text.
    """
    config_fingerprint = resolved.config_fingerprint()

    extraction_query = (
        select(PipelineRun, ExtractionRunInput)
        .join(ExtractionRunInput, ExtractionRunInput.run_id == PipelineRun.id)
        .where(PipelineRun.stage == MENTION_EXTRACTION_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    )
    if scope_ids is not None:
        extraction_query = extraction_query.where(PipelineRun.scope_id.in_(scope_ids))
    extraction_rows = session.exec(extraction_query).all()
    if not extraction_rows:
        return {}

    extraction_scope_ids = [run.scope_id for run, _ in extraction_rows]

    chunking_rows = session.exec(
        select(PipelineRun.id, PipelineRun.scope_id, ChunkRunInput.chunk_set_fingerprint)
        .join(ChunkRunInput, ChunkRunInput.run_id == PipelineRun.id)
        .where(
            PipelineRun.stage == CHUNKING_STAGE,
            PipelineRun.status == RunStatus.ACTIVE,
            PipelineRun.scope_id.in_(extraction_scope_ids),
        )
    ).all()
    chunking_upstream: dict[UUID, tuple[int, str]] = {
        scope_id: (run_id, output_fingerprint) for run_id, scope_id, output_fingerprint in chunking_rows
    }

    variant_upstream: dict[UUID, tuple[int, str]] = {}
    if resolved.input_policy == INPUT_POLICY_CONTEXTUALIZED:
        variant_rows = session.exec(
            select(PipelineRun.id, PipelineRun.scope_id, ContextualizationRunInput.output_fingerprint)
            .join(ContextualizationRunInput, ContextualizationRunInput.run_id == PipelineRun.id)
            .where(
                PipelineRun.stage == VARIANT_STAGE,
                PipelineRun.status == RunStatus.ACTIVE,
                PipelineRun.scope_id.in_(extraction_scope_ids),
            )
        ).all()
        variant_upstream = {
            scope_id: (run_id, output_fingerprint) for run_id, scope_id, output_fingerprint in variant_rows
        }

    extraction_run_ids = [run.id for run, _ in extraction_rows]
    applicability_rows = session.exec(
        select(ExtractionRunApplicability.run_id, ExtractionRunApplicability.upstream_run_id).where(
            ExtractionRunApplicability.run_id.in_(extraction_run_ids)
        )
    ).all()
    applicable_upstreams: dict[int, set[int]] = {}
    for run_id, upstream_run_id in applicability_rows:
        applicable_upstreams.setdefault(run_id, set()).add(upstream_run_id)

    result: dict[UUID, Currentness] = {}
    for run, run_input in extraction_rows:
        upstream = variant_upstream.get(run.scope_id) or chunking_upstream.get(run.scope_id)
        if upstream is None:
            # No active chunking run for the source: nothing to classify against.
            continue
        upstream_run_id, upstream_output_fingerprint = upstream
        if (
            upstream_output_fingerprint != run_input.input_fingerprint
            or config_fingerprint != run_input.config_fingerprint
        ):
            result[run.scope_id] = Currentness.STALE
        elif run_input.upstream_run_id == upstream_run_id or upstream_run_id in applicable_upstreams.get(
            run.id, set()
        ):
            result[run.scope_id] = Currentness.CURRENT
        else:
            result[run.scope_id] = Currentness.NEEDS_RECONCILIATION
    return result


def classify_extraction_sources(
    session: "Session", *, resolved: "ResolvedExtractionConfig"
) -> dict[UUID, Currentness]:
    """Classify sources with active extraction runs.

    Sources without active chunking or extraction runs are absent. The mapping
    contains each classified source's ``scope_id`` and :class:`Currentness`.

    Args:
        session: Active, read-only session.
        resolved: The currently-resolved extraction configuration.

    Returns:
        The classification mapping.
    """
    return _classify_extraction(session, resolved=resolved)


def classify_extraction_source(
    session: "Session", source_id: UUID, *, resolved: "ResolvedExtractionConfig"
) -> Currentness | None:
    """Classify one source's active extraction run.

    Return ``None`` when no active chunking or extraction run exists. This
    source-scoped call uses a bounded query set.

    Args:
        session: Active, read-only session.
        source_id: The source's identity.
        resolved: The currently-resolved extraction configuration.
    """
    return _classify_extraction(session, resolved=resolved, scope_ids=[source_id]).get(source_id)


def stale_extraction_sources(session: "Session", *, resolved: "ResolvedExtractionConfig") -> set[UUID]:
    """Return source IDs whose active extraction run is ``STALE``.

    Stale sources are not admitted automatically. Sources in
    ``NEEDS_RECONCILIATION`` are excluded.

    Args:
        session: Active, read-only session.
        resolved: The currently-resolved extraction configuration.

    Returns:
        The stale source IDs.
    """
    return {
        source_id
        for source_id, currentness in classify_extraction_sources(session, resolved=resolved).items()
        if currentness is Currentness.STALE
    }


def record_extraction_run_applicability(
    session: "Session",
    *,
    run_id: int,
    upstream_run_id: int,
    input_policy: InputPolicy,
    resolved: "ResolvedExtractionConfig",
) -> ExtractionRunApplicability:
    """Record applicability of an extraction run to an upstream run.

    The upstream fingerprint is read from persisted provenance. The relation is
    append-only and idempotent; an existing identical relation is returned. The
    function does not commit.

    Raises:
        ValueError: If either run is invalid, provenance is missing, the
            derivation key does not match, or an existing relation conflicts.
    """
    run = session.get(PipelineRun, run_id)
    if run is None or run.status != RunStatus.ACTIVE or run.stage != MENTION_EXTRACTION_STAGE:
        raise ValueError(
            f"run {run_id!r} is not an active {MENTION_EXTRACTION_STAGE!r} run; "
            "applicability may only be recorded against an active extraction run"
        )

    upstream = session.get(PipelineRun, upstream_run_id)
    if upstream is None or upstream.stage not in (CHUNKING_STAGE, VARIANT_STAGE) or upstream.scope_id != run.scope_id:
        raise ValueError(
            f"upstream run {upstream_run_id!r} does not resolve to a chunking or contextualization run "
            f"sharing scope_id={run.scope_id!r}"
        )

    upstream_output_fingerprint = _upstream_output_fingerprint(session, upstream)
    candidate_key = extraction_derivation_key(
        input_fingerprint=upstream_output_fingerprint, config_fingerprint=resolved.config_fingerprint()
    )
    if candidate_key != run.derivation_key:
        raise ValueError(
            f"the candidate key computed from upstream run {upstream_run_id!r}'s output and the "
            f"currently-resolved configuration does not match run {run_id!r}'s derivation_key"
        )

    run_input = session.get(ExtractionRunInput, run_id)
    if run_input is None:
        raise ValueError(f"run {run_id!r} has no recorded ExtractionRunInput; its persist is not complete")

    existing = session.get(ExtractionRunApplicability, (run_id, upstream_run_id))
    if existing is not None:
        if existing.input_policy != input_policy or existing.input_fingerprint != upstream_output_fingerprint:
            raise ValueError(
                f"applicability ({run_id!r}, {upstream_run_id!r}) is already recorded with a different "
                "input_policy or input_fingerprint; there is no update path"
            )
        return existing

    applicability = ExtractionRunApplicability(
        run_id=run_id,
        upstream_run_id=upstream_run_id,
        input_policy=input_policy,
        input_fingerprint=upstream_output_fingerprint,
    )
    session.add(applicability)
    return applicability


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
) -> DocumentExtractionResult:
    """Extract and persist one source in one document transaction.

    The function calls the extractor at most once per chunk. It reuses the active
    run and records applicability when the resolved payload and configuration
    match. Otherwise it opens a superseding run and persists the run input,
    mentions, and co-occurrences.

    The read and extraction phases run outside the write transaction. Model calls
    do not hold the writer lock. A stale plan raises without committing.

    Args:
        engine: Shared engine for the read and write sessions.
        source_id: Source identity for the extraction run.
        extractor: Injected NER extractor.
        input_policy: Raw or contextualized input policy.

    Returns:
        The active run ID and output counts.

    Raises:
        ValueError: If the input policy is invalid or the source has no active
            chunking run.
        StalePlanError: If planned upstream state changes before persistence.
    """
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


def extract_corpus(
    engine: "Engine",
    *,
    source_ids: "Sequence[UUID]",
    extractor: EntityExtractor,
    input_policy: InputPolicy,
) -> list[DocumentExtractionResult]:
    """Extract sources in the given order, one document transaction at a time.

    Each source commits independently. A failure propagates after prior sources
    commit, and the function returns no partial result list. Repeating the same
    inputs is safe because each document operation is idempotent.

    Args:
        engine: Shared engine passed to each document operation.
        source_ids: Sources to process in order.
        extractor: Shared injected NER extractor.
        input_policy: Input policy shared by all sources.

    Returns:
        One result per source, in ``source_ids`` order.
    """
    return [
        extract_document(engine, source_id=source_id, extractor=extractor, input_policy=input_policy)
        for source_id in source_ids
    ]
