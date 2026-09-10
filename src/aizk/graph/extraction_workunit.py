"""Enqueue extraction work-units for source identities.

Incremental and bulk paths deduplicate on ``idempotency_key`` and respect stage
capacity. Pending-work selection is read-only and shared by admission and
operator surfaces. Existing terminal units are reused; re-extraction requeues
the existing source unit through the operator action.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlmodel import func, select

from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_events import (
    EXTRACTION_STAGE,
    CancelledPayload,
    ClaimedPayload,
    ExtractionEventKind,
    FailedPayload,
    QueuedPayload,
    RecoveredStalePayload,
    RequeuedPayload,
    TimedOutPayload,
)
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.capacity import require_positive_limit, within_headroom
from aizk.pipeline.invalidation import require_reprocessing_confirmation
from aizk.pipeline.run import PipelineRun, RunStatus
from aizk.pipeline.stage import StageDefinition, enqueue

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Select
    from sqlmodel import Session

    from aizk.pipeline.events import CreationOrigin

logger = logging.getLogger(__name__)


class ExtractionParams(BaseModel):
    """The stage-specific input of one extraction unit: the source to extract.

    The field name is the work-unit table's column name.
    """

    model_config = ConfigDict(frozen=True)
    source_id: UUID


def _idempotency_key(source_id: UUID) -> str:
    """Return the source-scoped enqueue idempotency key.

    The key remains stable across work-unit status and upstream generation changes.
    """
    return f"source:{source_id}"


def enqueue_extraction(
    session: "Session",
    *,
    source_id: UUID,
    queue_max_depth: int,
    origin: "CreationOrigin",
    principal: str,
) -> tuple[ExtractionJob, bool]:
    """Enqueue or reuse one source's extraction work-unit.

    An existing unit is returned unchanged in any status. Otherwise the function
    checks capacity, stages a ``QUEUED`` unit and its creation event, and does not
    commit. Existing units bypass capacity checks.

    Args:
        session: Active session; the caller owns commit and rollback.
        source_id: Source identity to extract.
        queue_max_depth: Maximum actionable backlog; ``0`` means unlimited.
        origin: Creation path recorded in the event.
        principal: Acting principal recorded in the event.

    Returns:
        The existing or newly-created work-unit and whether this call created it.

    Raises:
        StageAtCapacityError: If capacity is reached for a new unit.
    """
    job, created = enqueue(
        session,
        EXTRACTION_DEFINITION,
        ExtractionParams(source_id=source_id),
        origin=origin,
        principal=principal,
        queue_max_depth=queue_max_depth,
    )
    if not created:
        logger.debug("Reusing extraction work-unit id=%s for source_id=%s", job.id, source_id)
        return job, False
    logger.debug("Enqueued extraction work-unit id=%s for source_id=%s", job.id, source_id)
    return job, True


def _sources_with_active_chunking_run(session: "Session") -> list[UUID]:
    """Return source IDs with active chunking runs in run-creation order.

    Sources without a chunking run are excluded because they have nothing to
    extract.
    """
    return list(
        session.exec(
            select(PipelineRun.scope_id)
            .where(PipelineRun.stage == CHUNKING_STAGE, PipelineRun.status == RunStatus.ACTIVE)
            .order_by(PipelineRun.created_at)
        ).all()
    )


def _enqueued_sources(session: "Session", source_ids: "Sequence[UUID]") -> set[UUID]:
    """Return which of ``source_ids`` already have an extraction work-unit."""
    if not source_ids:
        return set()
    return set(session.exec(select(ExtractionJob.source_id).where(ExtractionJob.source_id.in_(source_ids))).all())


def _pending_extraction_query() -> "Select":
    """Return the query for sources with active chunking runs and no work-unit."""
    return (
        select(PipelineRun.scope_id)
        .where(PipelineRun.stage == CHUNKING_STAGE, PipelineRun.status == RunStatus.ACTIVE)
        .outerjoin(ExtractionJob, ExtractionJob.source_id == PipelineRun.scope_id)
        .where(ExtractionJob.id.is_(None))
        .order_by(PipelineRun.created_at, PipelineRun.scope_id)
    )


def pending_extraction_sources(session: "Session", *, limit: int | None = None) -> list[UUID]:
    """Return pending source IDs in chunking-run creation order.

    A source is pending when it has an active chunking run and no extraction
    work-unit, regardless of the existing unit status. ``limit`` applies after
    this filter and must be positive. The result reflects current database state.

    Args:
        session: Active, read-only session.
        limit: Maximum number of pending sources.

    Returns:
        The pending source IDs.

    Raises:
        ValueError: If ``limit`` is less than one.
    """
    require_positive_limit(limit)
    query = _pending_extraction_query()
    if limit is not None:
        query = query.limit(limit)
    return list(session.exec(query).all())


def pending_extraction_count(session: "Session") -> int:
    """Return the number of pending extraction sources."""
    return session.exec(select(func.count()).select_from(_pending_extraction_query().subquery())).one()


def _pending_extraction_params(session: "Session", limit: int | None) -> list[ExtractionParams]:
    """Return the parameters of the pending extraction units, up to ``limit``."""
    return [ExtractionParams(source_id=source_id) for source_id in pending_extraction_sources(session, limit=limit)]


#: The extraction stage's static definition.
EXTRACTION_DEFINITION: StageDefinition[ExtractionJob, ExtractionParams] = StageDefinition(
    name=EXTRACTION_STAGE,
    params=ExtractionParams,
    unit_key=lambda params: _idempotency_key(params.source_id),
    work_unit_model=ExtractionJob,
    event_kinds=ExtractionEventKind,
    queued_payload=QueuedPayload,
    claimed_payload=ClaimedPayload,
    recovered_stale_payload=RecoveredStalePayload,
    failed_payload=FailedPayload,
    cancelled_payload=CancelledPayload,
    timed_out_payload=TimedOutPayload,
    requeued_payload=RequeuedPayload,
    pending=_pending_extraction_params,
)


def enqueue_extraction_backfill(
    session: "Session",
    *,
    confirmed: bool = False,
    queue_max_depth: int,
    principal: str,
) -> list[ExtractionJob]:
    """Enqueue or reuse work-units for eligible sources.

    Eligible sources have active chunking runs. The operation requires
    ``confirmed=True`` and admits only sources within the available queue
    headroom. It does not commit.

    Args:
        session: Active session; the caller owns commit and rollback.
        confirmed: Approval for the corpus-wide operation.
        queue_max_depth: Maximum actionable backlog; ``0`` means unlimited.
        principal: Operator identity recorded on created events.

    Returns:
        The enqueued or reused work-units.

    Raises:
        ReprocessingConfirmationError: If ``confirmed`` is false.
    """
    require_reprocessing_confirmation("corpus-wide extraction backfill", confirmed=confirmed)
    eligible = _sources_with_active_chunking_run(session)
    admitted = within_headroom(
        session,
        ExtractionJob,
        eligible,
        stage=EXTRACTION_STAGE,
        limit=queue_max_depth,
        already_enqueued=_enqueued_sources(session, eligible),
    )
    return [
        enqueue_extraction(
            session, source_id=source_id, queue_max_depth=queue_max_depth, origin="backfill", principal=principal
        )[0]
        for source_id in admitted
    ]
