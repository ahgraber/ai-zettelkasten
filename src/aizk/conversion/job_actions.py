"""Operator retry/cancel commands for conversion jobs, as domain helpers.

These lift the retry and cancel transitions out of the JSON API route module so
every operator surface — the conversion JSON API and the operator console — calls
the same domain code rather than importing another app's route internals. Each
helper performs the status-eligibility check (raising :class:`ValueError` with an
operator-facing reason when ineligible), applies the field mutations, and
co-commits the matching lifecycle event via
:func:`aizk.conversion.datamodel.events.record_transition`. The caller owns the
surrounding ``BEGIN IMMEDIATE`` transaction; these helpers do **not** commit.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlmodel import select

from aizk.conversion.datamodel.events import (
    CancelledPayload,
    ConversionEventKind,
    QueuedPayload,
    record_transition,
)
from aizk.conversion.datamodel.job import ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.conversion.datamodel.job import ConversionJob

#: Native statuses a retry may re-queue from.
RETRYABLE_STATUSES = frozenset(
    {
        ConversionJobStatus.FAILED_RETRYABLE,
        ConversionJobStatus.FAILED_PERM,
        ConversionJobStatus.CANCELLED,
    }
)
#: Native statuses a cancel may terminate from.
CANCELLABLE_STATUSES = frozenset(
    {
        ConversionJobStatus.QUEUED,
        ConversionJobStatus.RUNNING,
        ConversionJobStatus.FAILED_RETRYABLE,
    }
)
#: Native statuses a delete may remove from (the terminal, non-active statuses).
DELETABLE_STATUSES = frozenset(
    {
        ConversionJobStatus.FAILED_RETRYABLE,
        ConversionJobStatus.FAILED_PERM,
        ConversionJobStatus.CANCELLED,
    }
)


def apply_job_retry(
    session: "Session",
    job: "ConversionJob",
    now: dt.datetime,
    *,
    submitted_by: str | None,
) -> None:
    """Re-queue a conversion job, clearing error/retry-wait fields and recording the event.

    Raises :class:`ValueError` (``"job_not_retryable"``) when the job is not in a
    re-queueable status. Increments the attempt count, records a ``queued``
    transition, and resets the timing/error fields so the worker re-claims it.
    """
    if job.status not in RETRYABLE_STATUSES:
        raise ValueError("job_not_retryable")
    job.attempts += 1
    record_transition(
        session,
        job,
        to_status=ConversionJobStatus.QUEUED,
        kind=ConversionEventKind.QUEUED,
        attempt=job.attempts,
        payload=QueuedPayload(
            submitted_by=submitted_by,
            requeue_reason="retry_endpoint",
        ),
    )
    job.earliest_next_attempt_at = None
    job.last_error_at = None
    job.error_code = None
    job.error_message = None
    job.queued_at = now
    job.started_at = None
    job.finished_at = None
    job.updated_at = now


def apply_job_cancel(
    session: "Session",
    job: "ConversionJob",
    now: dt.datetime,
    *,
    cancelled_by: str | None,
) -> None:
    """Cancel a conversion job, writing a terminal ``CANCELLED`` status and recording the event.

    Raises :class:`ValueError` (``"job_not_cancellable"``) when the job is not in a
    cancellable status. Records a ``cancelled`` transition and stamps the finish time.
    """
    if job.status not in CANCELLABLE_STATUSES:
        raise ValueError("job_not_cancellable")
    record_transition(
        session,
        job,
        to_status=ConversionJobStatus.CANCELLED,
        kind=ConversionEventKind.CANCELLED,
        attempt=job.attempts,
        payload=CancelledPayload(
            cancelled_by=cancelled_by,
            cancellation_reason=None,
        ),
    )
    job.finished_at = now
    job.earliest_next_attempt_at = None
    job.updated_at = now


def apply_job_delete(session: "Session", job: "ConversionJob") -> None:
    """Delete a terminal conversion job and its output row.

    Raises :class:`ValueError` (``"job_not_deletable"``) when the job is not in a
    deletable terminal status. This is a hard delete, not a lifecycle transition:
    it removes the job's :class:`ConversionOutput` (when present) and the job row
    itself, recording no event. The job's prior lifecycle events are left intact.
    """
    if job.status not in DELETABLE_STATUSES:
        raise ValueError("job_not_deletable")
    output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job.id)).first()
    if output:
        session.delete(output)
    session.delete(job)
