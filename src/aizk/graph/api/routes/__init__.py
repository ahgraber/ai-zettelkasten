"""Provide contextualization work-unit routes.

Submission, listing, retry, and cancellation use the graph work-unit domain and
co-commit lifecycle events with status changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from sqlalchemy import func, text
from sqlmodel import select

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.api.schemas import QueueFullResponse
from aizk.conversion.auth.principal import Principal
from aizk.graph.api.dependencies import get_admission_config, get_db_session
from aizk.graph.api.schemas import (
    ContextualizationJobList,
    ContextualizationJobResponse,
    ContextualizationSubmission,
)
from aizk.graph.capacity import StageAtCapacityError
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.enqueue import enqueue_output
from aizk.graph.job_actions import (
    apply_contextualization_cancel as _apply_cancel,
    apply_contextualization_retry as _apply_retry,
)
from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.graph.config import AdmissionConfig

#: Resolves the request principal (trust-network: a single deployment principal).
#: Required on every route for auth parity with the conversion API; the graph
#: stage is internal/post-conversion and does not owner-scope its work-units.
_Principal = Annotated[Principal, Depends(get_principal)]

router = APIRouter(prefix="/v1/contextualizations", tags=["contextualizations"])


def _get_or_404(session: "Session", job_id: int) -> ContextualizationJob:
    """Return the work-unit or raise 404."""
    job = session.get(ContextualizationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"contextualization work-unit {job_id} not found")
    return job


#: The capacity refusal, declared so a generated client knows to read the header
#: rather than discovering it at runtime. Each intake route adds its own 200 and
#: 404 to this, since both are returned from the handler body and neither is
#: inferable from the decorator's ``status_code``.
INTAKE_RESPONSES: dict[int | str, dict[str, Any]] = {
    503: {
        "model": QueueFullResponse,
        "description": "Stage is at capacity",
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before resubmitting.",
                "schema": {"type": "integer"},
            }
        },
    },
}


def queue_full_response(retry_after_seconds: int) -> JSONResponse:
    """Return a 503 capacity response with ``Retry-After`` and the standard body."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Queue is at capacity", "retry_after": retry_after_seconds},
        headers={"Retry-After": str(retry_after_seconds)},
    )


@router.post(
    "",
    response_model=ContextualizationJobResponse,
    status_code=status.HTTP_201_CREATED,
    responses=INTAKE_RESPONSES
    | {
        200: {"model": ContextualizationJobResponse, "description": "Work was already enqueued"},
        404: {"description": "No such conversion output"},
    },
)
def submit_job(
    submission: ContextualizationSubmission,
    api_response: Response,
    session: Annotated["Session", Depends(get_db_session)],
    admission_config: Annotated["AdmissionConfig", Depends(get_admission_config)],
    principal: _Principal,
) -> ContextualizationJobResponse | JSONResponse:
    """Submit one converted document for contextualization.

    Returns 201 for a new unit, 200 for an existing unit, 404 when the output is
    missing, and 503 when the stage is at capacity.
    """
    session.exec(text("BEGIN IMMEDIATE"))
    try:
        job, created = enqueue_output(
            session,
            submission.conversion_output_id,
            queue_max_depth=admission_config.contextualization_queue_max_depth,
            origin="intake",
            principal=principal.subject,
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StageAtCapacityError:
        session.rollback()
        return queue_full_response(admission_config.queue_retry_after_seconds)
    session.commit()
    session.refresh(job)
    if not created:
        api_response.status_code = status.HTTP_200_OK
    return ContextualizationJobResponse.model_validate(job)


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
    return ContextualizationJobResponse.model_validate(job)


@router.post("/{job_id}/cancel", response_model=ContextualizationJobResponse)
def cancel_job(
    job_id: int,
    session: Annotated["Session", Depends(get_db_session)],
    _principal: _Principal,
) -> ContextualizationJobResponse:
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
    return ContextualizationJobResponse.model_validate(job)
