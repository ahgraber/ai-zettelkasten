"""Operator routes for contextualization work-units: submit, list, detail, retry, cancel.

Submission resolves the referenced conversion output and calls the stage's domain
enqueue in the request transaction, so an intake-created unit is identical to one
created by any other path. Capacity is enforced at that enqueue seam rather than
in front of this one caller, so a full stage is refused with the same 503 and
``Retry-After`` the conversion service uses.

Read endpoints query the work-unit table; the retry and cancel mutations run in a
``BEGIN IMMEDIATE`` transaction and co-commit a transition event via
:func:`aizk.pipeline.events.record_transition`, so a status change never exists
without its audit event. Retry re-queues a terminal unit (so the worker re-claims
it); cancel writes a terminal ``CANCELLED`` status (cooperative for a running
unit — the worker resolves the outcome from its slot state).
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
    """Return the fleet's capacity refusal: 503 carrying ``Retry-After``.

    The body matches the conversion service's
    :class:`~aizk.conversion.api.schemas.QueueFullResponse`, so a client backs off
    the same way whichever service refused it.
    """
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
    _principal: _Principal,
) -> ContextualizationJobResponse | JSONResponse:
    """Submit one converted document for contextualization.

    Answers 201 with the created work-unit, 200 with the existing one when the
    output is already enqueued, 404 when no such output exists, and 503 when the
    stage is at capacity.
    """
    session.exec(text("BEGIN IMMEDIATE"))
    existing = session.exec(
        select(ContextualizationJob).where(
            ContextualizationJob.conversion_output_id == submission.conversion_output_id
        )
    ).first()
    try:
        job = enqueue_output(
            session,
            submission.conversion_output_id,
            queue_max_depth=admission_config.contextualization_queue_max_depth,
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StageAtCapacityError:
        session.rollback()
        return queue_full_response(admission_config.queue_retry_after_seconds)
    session.commit()
    session.refresh(job)
    if existing is not None:
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
