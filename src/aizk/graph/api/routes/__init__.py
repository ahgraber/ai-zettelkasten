"""Operator routes for contextualization work-units: list, detail, retry, cancel.

Read endpoints query the work-unit table; the retry and cancel mutations run in a
``BEGIN IMMEDIATE`` transaction and co-commit a transition event via
:func:`aizk.pipeline.events.record_transition`, so a status change never exists
without its audit event. Retry re-queues a terminal unit (so the worker re-claims
it); cancel writes a terminal ``CANCELLED`` status (cooperative for a running
unit — the worker resolves the outcome from its slot state).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Annotated

from sqlalchemy import func, text
from sqlmodel import select

from fastapi import APIRouter, Depends, HTTPException, Query

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth.principal import Principal
from aizk.graph.api.dependencies import get_db_session
from aizk.graph.api.schemas import ContextualizationJobList, ContextualizationJobResponse
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.events import CONTEXTUALIZATION_STAGE, CancelledPayload, GraphEventKind, RequeuedPayload
from aizk.pipeline.events import record_transition
from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from sqlmodel import Session

#: Resolves the request principal (trust-network: a single deployment principal).
#: Required on every route for auth parity with the conversion API; the graph
#: stage is internal/post-conversion and does not owner-scope its work-units.
_Principal = Annotated[Principal, Depends(get_principal)]

router = APIRouter(prefix="/v1/contextualizations", tags=["contextualizations"])

#: Terminal statuses an operator may re-queue.
_RETRYABLE_FROM = frozenset({WorkUnitStatus.FAILED, WorkUnitStatus.CANCELLED, WorkUnitStatus.TIMED_OUT})
#: Statuses an operator may cancel.
_CANCELLABLE_FROM = frozenset({WorkUnitStatus.QUEUED, WorkUnitStatus.RUNNING, WorkUnitStatus.FAILED})


def _utcnow() -> datetime.datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc)


def _get_or_404(session: "Session", job_id: int) -> ContextualizationJob:
    """Return the work-unit or raise 404."""
    job = session.get(ContextualizationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"contextualization work-unit {job_id} not found")
    return job


def _apply_retry(session: "Session", job: ContextualizationJob) -> None:
    """Re-queue a terminal work-unit, clearing error/retry-wait fields and recording the event.

    Performs the status-eligibility check and raises :class:`ValueError` when the
    unit is not in a re-queueable terminal status (the message is the operator-facing
    reason). Applies the field mutations and co-commits a ``requeued`` transition via
    :func:`aizk.pipeline.events.record_transition`. The caller owns the surrounding
    transaction; this helper does **not** commit.
    """
    if job.status not in _RETRYABLE_FROM:
        raise ValueError(f"cannot retry a work-unit in status {job.status.value!r}")
    now = _utcnow()
    job.error_code = None
    job.error_message = None
    job.earliest_next_attempt_at = None
    job.finished_at = None
    job.queued_at = now
    job.updated_at = now
    record_transition(
        session,
        job,
        stage=CONTEXTUALIZATION_STAGE,
        work_unit_ref=str(job.id),
        aizk_uuid=job.aizk_uuid,
        to_status=WorkUnitStatus.QUEUED,
        kind=GraphEventKind.REQUEUED,
        attempt=job.attempts,
        payload=RequeuedPayload(requeue_reason="operator_retry"),
    )


def _apply_cancel(session: "Session", job: ContextualizationJob) -> None:
    """Cancel a work-unit, writing a terminal ``CANCELLED`` status and recording the event.

    Performs the status-eligibility check and raises :class:`ValueError` when the
    unit is not in a cancellable status (the message is the operator-facing reason).
    Applies the field mutations and co-commits a ``cancelled`` transition via
    :func:`aizk.pipeline.events.record_transition`. The caller owns the surrounding
    transaction; this helper does **not** commit.
    """
    if job.status not in _CANCELLABLE_FROM:
        raise ValueError(f"cannot cancel a work-unit in status {job.status.value!r}")
    now = _utcnow()
    job.finished_at = now
    job.updated_at = now
    record_transition(
        session,
        job,
        stage=CONTEXTUALIZATION_STAGE,
        work_unit_ref=str(job.id),
        aizk_uuid=job.aizk_uuid,
        to_status=WorkUnitStatus.CANCELLED,
        kind=GraphEventKind.CANCELLED,
        attempt=job.attempts,
        payload=CancelledPayload(cancellation_reason="operator_cancel"),
    )


@router.get("", response_model=ContextualizationJobList)
def list_jobs(
    session: Annotated["Session", Depends(get_db_session)],
    _principal: _Principal,
    status: Annotated[WorkUnitStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ContextualizationJobList:
    """List contextualization work-units, newest first, optionally filtered by status."""
    count_stmt = select(func.count()).select_from(ContextualizationJob)
    page_stmt = select(ContextualizationJob).order_by(ContextualizationJob.created_at.desc())  # type: ignore[attr-defined]
    if status is not None:
        count_stmt = count_stmt.where(ContextualizationJob.status == status)
        page_stmt = page_stmt.where(ContextualizationJob.status == status)

    total = session.exec(count_stmt).one()
    jobs = session.exec(page_stmt.limit(limit).offset(offset)).all()
    return ContextualizationJobList(
        jobs=[ContextualizationJobResponse.model_validate(job) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=ContextualizationJobResponse)
def get_job(
    job_id: int,
    session: Annotated["Session", Depends(get_db_session)],
    _principal: _Principal,
) -> ContextualizationJobResponse:
    """Return one contextualization work-unit, or 404 if it does not exist."""
    return ContextualizationJobResponse.model_validate(_get_or_404(session, job_id))


@router.post("/{job_id}/retry", response_model=ContextualizationJobResponse)
def retry_job(
    job_id: int,
    session: Annotated["Session", Depends(get_db_session)],
    _principal: _Principal,
) -> ContextualizationJobResponse:
    """Re-queue a terminal work-unit so the worker re-claims it.

    Rejects (409) a unit that is not in a re-queueable terminal status. Clears the
    error/retry-wait fields and records a ``requeued`` transition event.
    """
    session.exec(text("BEGIN IMMEDIATE"))
    job = _get_or_404(session, job_id)
    try:
        _apply_retry(session, job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    session.refresh(job)
    return ContextualizationJobResponse.model_validate(job)


@router.post("/{job_id}/cancel", response_model=ContextualizationJobResponse)
def cancel_job(
    job_id: int,
    session: Annotated["Session", Depends(get_db_session)],
    _principal: _Principal,
) -> ContextualizationJobResponse:
    """Cancel a work-unit, writing a terminal ``CANCELLED`` status.

    Rejects (409) a unit not in a cancellable status. For a running unit this is
    cooperative — the worker resolves the outcome from its slot state — but the
    operator-visible status flips immediately.
    """
    session.exec(text("BEGIN IMMEDIATE"))
    job = _get_or_404(session, job_id)
    try:
        _apply_cancel(session, job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    session.refresh(job)
    return ContextualizationJobResponse.model_validate(job)
