"""Conversion's claim and stale-recovery query helpers.

The two session-scoped query helpers
:class:`~aizk.conversion.handler.ConversionStageHandler` runs to drive the
conversion stage under the pipeline runner: the claim query + transition and the
stale-recovery query + transition. Both run inside a caller-owned transaction and
never commit.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

from sqlmodel import Session, select

from aizk.conversion.datamodel.events import (
    ClaimedPayload,
    ConversionEventKind,
    RecoveredStalePayload,
    record_transition,
)
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.processing.types import _utcnow
from aizk.conversion.utilities.config import ConversionConfig

logger = logging.getLogger(__name__)


def recover_stale_in_session(session: Session, config: ConversionConfig) -> list[int]:
    """Reclaim jobs stranded in RUNNING past the stale threshold, inside ``session``.

    Selects RUNNING jobs whose ``started_at`` predates the stale threshold,
    transitions each to FAILED_RETRYABLE, and records a ``recovered_stale``
    event via :func:`record_transition`. Returns the recovered job ids.

    Caller owns the transaction boundary: this helper stages its writes through
    ``session.add`` (via ``record_transition``) and does NOT commit. This is the
    single source of truth for the stale-recovery query + transition, used by
    the pipeline runner adapter (which passes its own session).
    """
    now = _utcnow()
    stale_before = now - dt.timedelta(minutes=config.worker_stale_job_minutes)

    jobs = session.exec(
        select(ConversionJob)
        .where(ConversionJob.status == ConversionJobStatus.RUNNING)
        .where(ConversionJob.started_at.is_not(None))  # type: ignore[operator]
        .where(ConversionJob.started_at < stale_before)
    ).all()

    recovered: list[int] = []
    for job in jobs:
        last_started_at = job.started_at
        job.earliest_next_attempt_at = now
        job.error_code = "worker_stale_running"
        job.error_message = f"Marked stale after {config.worker_stale_job_minutes} minutes without completion."
        job.last_error_at = now
        job.updated_at = now
        record_transition(
            session,
            job,
            to_status=ConversionJobStatus.FAILED_RETRYABLE,
            kind=ConversionEventKind.RECOVERED_STALE,
            attempt=job.attempts,
            payload=RecoveredStalePayload(
                stale_after_minutes=config.worker_stale_job_minutes,
                last_started_at=last_started_at,
            ),
        )
        if job.id is not None:
            recovered.append(job.id)

    return recovered


def claim_next_in_session(session: Session) -> int | None:
    """Claim the next eligible job and transition it to RUNNING, inside ``session``.

    Selects the oldest eligible job (QUEUED or FAILED_RETRYABLE past its
    retry-wait), transitions it to RUNNING, **post-increments ``attempts``**
    (the claim is the attempt counter's source of truth), and records a
    ``claimed`` event via :func:`record_transition`. Returns the claimed job id,
    or ``None`` when no job is eligible.

    Caller owns the transaction boundary: the caller must have opened
    ``BEGIN IMMEDIATE`` (so concurrent workers cannot select the same job) and
    owns commit/rollback; this helper does NOT commit. This is the single source
    of truth for the claim query + transition, used by the pipeline runner
    adapter (which passes its own ``BEGIN IMMEDIATE`` session).
    """
    now = _utcnow()
    job = session.exec(
        select(ConversionJob)
        .where(ConversionJob.status.in_([ConversionJobStatus.QUEUED, ConversionJobStatus.FAILED_RETRYABLE]))
        .where(
            (ConversionJob.earliest_next_attempt_at.is_(None))  # type: ignore[operator]
            | (ConversionJob.earliest_next_attempt_at <= now)
        )
        .order_by(ConversionJob.queued_at)
    ).first()

    if not job:
        return None

    job.started_at = now
    job.attempts += 1
    job.updated_at = now
    record_transition(
        session,
        job,
        to_status=ConversionJobStatus.RUNNING,
        kind=ConversionEventKind.CLAIMED,
        attempt=job.attempts,
        payload=ClaimedPayload(claimed_at=now, worker_pid=os.getpid()),
    )
    return job.id
