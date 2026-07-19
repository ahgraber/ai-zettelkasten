"""Operator routes for extraction work-units: list, detail, retry, cancel.

Mirrors ``aizk.graph.api.routes`` (the contextualization work-unit routes)
exactly in structure and behavior, parameterized for
:class:`~aizk.graph.datamodel.ExtractionJob`. Read endpoints query the
work-unit table; the retry and cancel mutations run in a ``BEGIN IMMEDIATE``
transaction and co-commit a transition event via
:func:`aizk.pipeline.events.record_transition`, so a status change never
exists without its audit event. Retry re-queues a terminal unit (so the worker
re-claims it); cancel writes a terminal ``CANCELLED`` status (cooperative for a
running unit — the worker resolves the outcome from its slot state).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from sqlalchemy import func, text
from sqlmodel import select

from fastapi import APIRouter, Depends, HTTPException, Query

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth.principal import Principal
from aizk.graph.api.dependencies import get_db_session
from aizk.graph.api.schemas import ExtractionJobList, ExtractionJobResponse
from aizk.graph.datamodel import ExtractionJob
from aizk.graph.job_actions import apply_extraction_cancel as _apply_cancel, apply_extraction_retry as _apply_retry
from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from sqlmodel import Session

#: Resolves the request principal (trust-network: a single deployment principal).
#: Required on every route for auth parity with the conversion API; the graph
#: stage is internal/post-conversion and does not owner-scope its work-units.
_Principal = Annotated[Principal, Depends(get_principal)]

router = APIRouter(prefix="/v1/extractions", tags=["extractions"])


def _get_or_404(session: "Session", job_id: int) -> ExtractionJob:
    """Return the work-unit or raise 404."""
    job = session.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"extraction work-unit {job_id} not found")
    return job


@router.get("", response_model=ExtractionJobList)
def list_jobs(
    session: Annotated["Session", Depends(get_db_session)],
    _principal: _Principal,
    status: Annotated[WorkUnitStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExtractionJobList:
    """List extraction work-units, newest first, optionally filtered by status."""
    count_stmt = select(func.count()).select_from(ExtractionJob)
    page_stmt = select(ExtractionJob).order_by(ExtractionJob.created_at.desc())  # type: ignore[attr-defined]
    if status is not None:
        count_stmt = count_stmt.where(ExtractionJob.status == status)
        page_stmt = page_stmt.where(ExtractionJob.status == status)

    total = session.exec(count_stmt).one()
    jobs = session.exec(page_stmt.limit(limit).offset(offset)).all()
    return ExtractionJobList(
        jobs=[ExtractionJobResponse.model_validate(job) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=ExtractionJobResponse)
def get_job(
    job_id: int,
    session: Annotated["Session", Depends(get_db_session)],
    _principal: _Principal,
) -> ExtractionJobResponse:
    """Return one extraction work-unit, or 404 if it does not exist."""
    return ExtractionJobResponse.model_validate(_get_or_404(session, job_id))


@router.post("/{job_id}/retry", response_model=ExtractionJobResponse)
def retry_job(
    job_id: int,
    session: Annotated["Session", Depends(get_db_session)],
    _principal: _Principal,
) -> ExtractionJobResponse:
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
    return ExtractionJobResponse.model_validate(job)


@router.post("/{job_id}/cancel", response_model=ExtractionJobResponse)
def cancel_job(
    job_id: int,
    session: Annotated["Session", Depends(get_db_session)],
    _principal: _Principal,
) -> ExtractionJobResponse:
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
    return ExtractionJobResponse.model_validate(job)
