"""Provide extraction work-unit routes.

Submission, listing, retry, and cancellation use the extraction work-unit
domain and co-commit lifecycle events with status changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from sqlalchemy import func, text
from sqlmodel import select

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth.principal import Principal
from aizk.conversion.datamodel.source import Source
from aizk.graph.api.dependencies import get_admission_config, get_db_session
from aizk.graph.api.routes import INTAKE_RESPONSES, queue_full_response
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
    responses=INTAKE_RESPONSES
    | {
        200: {"model": ExtractionJobResponse, "description": "Work was already enqueued"},
        404: {"description": "No such source"},
    },
)
def submit_job(
    submission: ExtractionSubmission,
    api_response: Response,
    session: Annotated["Session", Depends(get_db_session)],
    admission_config: Annotated["AdmissionConfig", Depends(get_admission_config)],
    principal: _Principal,
) -> ExtractionJobResponse | JSONResponse:
    """Submit one source for entity-mention extraction.

    Returns 201 for a new unit, 200 for an existing unit, 404 when the source is
    missing, and 503 when the stage is at capacity.
    """
    session.exec(text("BEGIN IMMEDIATE"))
    # Resolved on the durable ``source_id`` identity, not the table's row surrogate.
    known_source = session.exec(select(Source).where(Source.source_id == submission.source_id)).first()
    if known_source is None:
        session.rollback()
        raise HTTPException(status_code=404, detail=f"source {submission.source_id} not found")
    try:
        job, created = enqueue_extraction(
            session,
            source_id=submission.source_id,
            queue_max_depth=admission_config.extraction_queue_max_depth,
            origin="intake",
            principal=principal.subject,
        )
    except StageAtCapacityError:
        session.rollback()
        return queue_full_response(admission_config.queue_retry_after_seconds)
    session.commit()
    session.refresh(job)
    if not created:
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
    """Re-queue a terminal work-unit, clear retry state, and record the event.

    Returns 409 when the status is not re-queueable.
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
    """Cancel a work-unit and record terminal ``CANCELLED`` status.

    Returns 409 when the status is not cancellable. Running units stop
    cooperatively.
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
