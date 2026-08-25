"""Operator routes for extraction work-units: submit, list, detail, retry, cancel.

Mirrors ``aizk.graph.api.routes`` (the contextualization work-unit routes)
exactly in structure and behavior, parameterized for
:class:`~aizk.graph.datamodel.ExtractionJob`. Submission resolves on the source
identity rather than a conversion output.

Read endpoints query the work-unit table; the retry and cancel mutations run in a
``BEGIN IMMEDIATE`` transaction and co-commit a transition event via
:func:`aizk.pipeline.events.record_transition`, so a status change never
exists without its audit event. Retry re-queues a terminal unit (so the worker
re-claims it); cancel writes a terminal ``CANCELLED`` status (cooperative for a
running unit — the worker resolves the outcome from its slot state).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from sqlalchemy import func, text
from sqlmodel import select

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.api.schemas import QueueFullResponse
from aizk.conversion.auth.principal import Principal
from aizk.conversion.datamodel.source import Source
from aizk.graph.api.dependencies import get_admission_config, get_db_session
from aizk.graph.api.routes import queue_full_response
from aizk.graph.api.schemas import ExtractionJobList, ExtractionJobResponse, ExtractionSubmission
from aizk.graph.capacity import StageAtCapacityError
from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_workunit import enqueue_extraction
from aizk.graph.job_actions import apply_extraction_cancel as _apply_cancel, apply_extraction_retry as _apply_retry
from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.graph.config import AdmissionConfig

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


@router.post(
    "",
    response_model=ExtractionJobResponse,
    status_code=status.HTTP_201_CREATED,
    responses={503: {"model": QueueFullResponse, "description": "Stage is at capacity"}},
)
def submit_job(
    submission: ExtractionSubmission,
    api_response: Response,
    session: Annotated["Session", Depends(get_db_session)],
    admission_config: Annotated["AdmissionConfig", Depends(get_admission_config)],
    _principal: _Principal,
) -> ExtractionJobResponse | JSONResponse:
    """Submit one source for entity-mention extraction.

    Answers 201 with the created work-unit, 200 with the existing one when the
    source is already enqueued, 404 when no such source exists, and 503 when the
    stage is at capacity.
    """
    session.exec(text("BEGIN IMMEDIATE"))
    # Resolved on the durable ``source_id`` identity, not the table's row surrogate.
    known_source = session.exec(select(Source).where(Source.source_id == submission.source_id)).first()
    if known_source is None:
        session.rollback()
        raise HTTPException(status_code=404, detail=f"source {submission.source_id} not found")
    existing = session.exec(select(ExtractionJob).where(ExtractionJob.source_id == submission.source_id)).first()
    try:
        job = enqueue_extraction(
            session,
            source_id=submission.source_id,
            queue_max_depth=admission_config.extraction_queue_max_depth,
        )
    except StageAtCapacityError:
        session.rollback()
        return queue_full_response(admission_config.queue_retry_after_seconds)
    session.commit()
    session.refresh(job)
    if existing is not None:
        api_response.status_code = status.HTTP_200_OK
    return ExtractionJobResponse.model_validate(job)


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
