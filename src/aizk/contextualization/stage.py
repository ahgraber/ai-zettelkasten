"""The contextualization stage's static definition, work-unit parameters, and pending-work queries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlmodel import func, select

from aizk.contextualization.events import (
    CONTEXTUALIZATION_STAGE,
    CancelledPayload,
    ClaimedPayload,
    FailedPayload,
    GraphEventKind,
    QueuedPayload,
    RecoveredStalePayload,
    RequeuedPayload,
    TimedOutPayload,
)
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.datamodel import ContextualizationJob
from aizk.pipeline.capacity import require_positive_limit
from aizk.pipeline.stage import StageDefinition

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlmodel import Session


class ContextualizationParams(BaseModel):
    """The stage-specific inputs of one contextualization unit.

    The field names are the work-unit table's column names.
    """

    model_config = ConfigDict(frozen=True)
    conversion_output_id: int
    source_id: UUID


def _idempotency_key(conversion_output_id: int) -> str:
    """Return the deduplication key for a conversion output.

    A new conversion output receives a distinct work unit.
    """
    return f"conversion_output:{conversion_output_id}"


#: The scan-limit check every pending query shares.
_require_bounding_limit = require_positive_limit


def _newest_output_per_source() -> "Select":
    """Return one newest conversion output per source in reproducible order.

    The greatest output id wins within a source. Results use ``created_at`` and
    ``source_id`` as cross-source ordering keys.
    """
    winners = select(func.max(ConversionOutput.id).label("output_id")).group_by(ConversionOutput.source_id).subquery()
    return (
        select(
            ConversionOutput.id.label("output_id"),
            ConversionOutput.source_id,
            ConversionOutput.created_at,
        )
        .join(winners, ConversionOutput.id == winners.c.output_id)
        .order_by(ConversionOutput.created_at, ConversionOutput.source_id)
    )


def _pending_contextualization_query() -> "Select":
    """Return newest-per-source outputs without a contextualization work-unit."""
    winners = _newest_output_per_source().subquery()
    return (
        select(winners.c.output_id, winners.c.source_id)
        .outerjoin(ContextualizationJob, ContextualizationJob.conversion_output_id == winners.c.output_id)
        .where(ContextualizationJob.id.is_(None))
        .order_by(winners.c.created_at, winners.c.source_id)
    )


def pending_contextualization(session: "Session", *, limit: int | None = None) -> list[tuple[int, "UUID"]]:
    """Return pending conversion outputs in selection order.

    Only the newest output per source is considered. The result contains
    ``(conversion_output_id, source_id)`` pairs; a bound applies after pending
    filtering.

    Raises:
        ValueError: If ``limit`` is supplied and is less than one.
    """
    _require_bounding_limit(limit)
    pending = _pending_contextualization_query()
    if limit is not None:
        pending = pending.limit(limit)
    return [(output_id, source_id) for output_id, source_id in session.exec(pending).all()]


def pending_contextualization_count(session: "Session") -> int:
    """Count sources pending contextualization."""
    return session.exec(select(func.count()).select_from(_pending_contextualization_query().subquery())).one()


def pending_contextualization_outputs(session: "Session", *, limit: int | None = None) -> list[int]:
    """Return pending conversion output ids in selection order."""
    return [output_id for output_id, _source_id in pending_contextualization(session, limit=limit)]


def pending_contextualization_sources(session: "Session", *, limit: int | None = None) -> list["UUID"]:
    """Return sources pending contextualization in selection order."""
    return [source_id for _output_id, source_id in pending_contextualization(session, limit=limit)]


def _pending_contextualization_params(session: "Session", limit: int | None) -> list[ContextualizationParams]:
    """Return the parameters of the pending contextualization units, up to ``limit``."""
    return [
        ContextualizationParams(conversion_output_id=conversion_output_id, source_id=source_id)
        for conversion_output_id, source_id in pending_contextualization(session, limit=limit)
    ]


#: The contextualization stage's static definition.
CONTEXTUALIZATION_DEFINITION: StageDefinition[ContextualizationJob, ContextualizationParams] = StageDefinition(
    name=CONTEXTUALIZATION_STAGE,
    params=ContextualizationParams,
    unit_key=lambda params: _idempotency_key(params.conversion_output_id),
    work_unit_model=ContextualizationJob,
    event_kinds=GraphEventKind,
    queued_payload=QueuedPayload,
    claimed_payload=ClaimedPayload,
    recovered_stale_payload=RecoveredStalePayload,
    failed_payload=FailedPayload,
    cancelled_payload=CancelledPayload,
    timed_out_payload=TimedOutPayload,
    requeued_payload=RequeuedPayload,
    pending=_pending_contextualization_params,
)
