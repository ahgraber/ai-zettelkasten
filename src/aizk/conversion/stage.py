"""The conversion stage's static definition, job parameters, and operator retry and cancel."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlmodel import select

from aizk.conversion.core.hashing import compute_idempotency_key
from aizk.conversion.datamodel.events import (
    STAGE,
    CancelledPayload,
    ClaimedPayload,
    ConversionEventKind,
    FailedPayload,
    QueuedPayload,
    RecoveredStalePayload,
    RequeuedPayload,
    TimedOutPayload,
)
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.settings import ConversionConfig, default_stale_job_minutes
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.repository import WorkUnitRepository
from aizk.pipeline.stage import StageDefinition

if TYPE_CHECKING:
    import datetime

    from sqlmodel import Session

#: The job phase during which a ``running`` job cannot be cancelled.
UPLOAD_PHASE = "upload"
#: The job phase a claim writes.
CONVERT_PHASE = "convert"


class ConversionJobParams(BaseModel):
    """The stage-specific inputs of one conversion job.

    The dumped field names are the job table's column names; the excluded
    fields are the idempotency-key inputs.
    """

    source_ref: str
    title: str
    payload_version: int
    source_ref_hash: str = Field(exclude=True)
    converter_name: str = Field(exclude=True)
    config_snapshot: dict[str, object] = Field(exclude=True)
    requested_key: str | None = Field(default=None, exclude=True)


def _unit_key(params: ConversionJobParams) -> str:
    return params.requested_key or compute_idempotency_key(
        params.source_ref_hash, params.converter_name, params.config_snapshot
    )


def _find_owned(session: "Session", key: str, principal: str) -> ConversionJob | None:
    return session.exec(
        select(ConversionJob).where(ConversionJob.idempotency_key == key).where(ConversionJob.owner_id == principal)
    ).first()


def _queued_payload(_row: ConversionJob, *, origin: str, principal: str) -> QueuedPayload:
    return QueuedPayload(submitted_by=principal, requeue_reason="initial")


def _recovered_stale_payload(
    _row: ConversionJob, *, stale_after_minutes: float, last_started_at: "datetime.datetime | None"
) -> RecoveredStalePayload:
    return RecoveredStalePayload(stale_after_minutes=int(stale_after_minutes), last_started_at=last_started_at)


def _cancelled_payload(
    _row: ConversionJob, *, cancellation_reason: str | None, principal: str | None = None
) -> CancelledPayload:
    return CancelledPayload(cancelled_by=principal, cancellation_reason=cancellation_reason)


def _timed_out_payload(row: ConversionJob, *, timeout_seconds: float) -> TimedOutPayload:
    return TimedOutPayload(timeout_seconds=timeout_seconds, phase=row.phase)


def _requeued_payload(_row: ConversionJob, *, requeue_reason: str, principal: str) -> RequeuedPayload:
    return RequeuedPayload(requeue_reason=requeue_reason, submitted_by=principal)


#: The conversion stage's static definition.
CONVERSION_DEFINITION: StageDefinition[ConversionJob, ConversionJobParams] = StageDefinition(
    name=STAGE,
    params=ConversionJobParams,
    unit_key=_unit_key,
    work_unit_model=ConversionJob,
    event_kinds=ConversionEventKind,
    queued_payload=_queued_payload,
    claimed_payload=ClaimedPayload,
    recovered_stale_payload=_recovered_stale_payload,
    failed_payload=FailedPayload,
    cancelled_payload=_cancelled_payload,
    timed_out_payload=_timed_out_payload,
    requeued_payload=_requeued_payload,
    find_existing=_find_owned,
)


def cancellable(job: ConversionJob) -> bool:
    """Report whether a ``running`` job may be cancelled: not once its upload has started."""
    return job.phase != UPLOAD_PHASE


def _clear_phase(_session: "Session", job: ConversionJob, _to_status: WorkUnitStatus) -> None:
    job.phase = None


_DEFAULTS: dict[str, Any] = {name: field.default for name, field in ConversionConfig.model_fields.items()}

_OPERATOR_REPOSITORY: WorkUnitRepository[ConversionJob] = WorkUnitRepository(
    CONVERSION_DEFINITION,
    retry_max_attempts=_DEFAULTS["worker_max_attempts"],
    retry_base_delay_seconds=_DEFAULTS["retry_base_delay_seconds"],
    stale_after_minutes=default_stale_job_minutes(
        _DEFAULTS["worker_job_timeout_seconds"], _DEFAULTS["worker_drain_timeout_seconds"]
    ),
    timeout_seconds=_DEFAULTS["worker_job_timeout_seconds"],
    on_transition=_clear_phase,
)


def retry_conversion_job(session: "Session", job: ConversionJob, *, principal: str) -> None:
    """Re-queue a terminal conversion job, keeping ``attempts`` and clearing every error field.

    Raises:
        IneligibleTransitionError: ``job_not_retryable`` for a status that is
            not ``failed``, ``cancelled``, or ``timed_out``.
    """
    _OPERATOR_REPOSITORY.requeue(session, job, reason="operator_retry", principal=principal)
    job.error_detail = None


def cancel_conversion_job(session: "Session", job: ConversionJob, *, principal: str) -> None:
    """Cancel a conversion job, keeping its error fields and clearing its retry wait.

    Raises:
        IneligibleTransitionError: ``job_not_cancellable`` for a terminal
            status other than ``failed``, or a ``running`` job in its upload
            phase.
    """
    _OPERATOR_REPOSITORY.cancel(session, job, reason=None, cancellable=cancellable, principal=principal)


__all__ = [
    "CONVERSION_DEFINITION",
    "CONVERT_PHASE",
    "UPLOAD_PHASE",
    "ConversionJobParams",
    "cancel_conversion_job",
    "cancellable",
    "retry_conversion_job",
]
