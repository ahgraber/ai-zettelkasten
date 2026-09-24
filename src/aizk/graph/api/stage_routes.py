"""Build the work-unit routes a graph stage registers: submit, list, detail, retry, and cancel."""

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from aizk.db.engine import begin_immediate_on
from aizk.pipeline.admission import AdmissionConfig
from aizk.pipeline.capacity import StageAtCapacityError
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.stage import StageDefinition


def _get_or_404(session: Session, model: type[Any], job_id: int, label: str) -> Any:
    """Return the work-unit or raise 404."""
    job = session.get(model, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"{label} work-unit {job_id} not found")
    return job


def stage_router(
    definition: StageDefinition[Any, Any],
    *,
    prefix: str,
    tag: str,
    label: str,
    submit_model: type[BaseModel],
    response_model: type[BaseModel],
    list_model: type[BaseModel],
    enqueue: Callable[..., tuple[Any, bool]],
    retry: Callable[..., None],
    cancel: Callable[..., None],
    capacity_field: str,
    submit_doc: str,
    submit_not_found_description: str,
    principal_dependency: Callable[..., Any],
    session_dependency: Callable[..., Any],
    admission_config_dependency: Callable[..., Any],
    intake_responses: dict[int | str, dict[str, Any]],
    queue_full_response: Callable[[int], JSONResponse],
    precheck: Callable[[Session, Any], None] | None = None,
    value_error_is_404: bool = False,
) -> APIRouter:
    """Return the submit, list, detail, retry, and cancel routes over ``definition.work_unit_model``."""
    model = definition.work_unit_model

    #: Resolves the request principal (trust-network: a single deployment principal).
    #: Required on every route for auth parity with the conversion API; the graph
    #: stage is internal/post-conversion and does not owner-scope its work-units.
    principal_annotation = Annotated[Any, Depends(principal_dependency)]

    router = APIRouter(prefix=prefix, tags=[tag])

    def submit_job(
        submission: submit_model,
        api_response: Response,
        session: Annotated[Session, Depends(session_dependency)],
        admission_config: Annotated[AdmissionConfig, Depends(admission_config_dependency)],
        principal: principal_annotation,
    ) -> Any:
        begin_immediate_on(session)
        if precheck is not None:
            precheck(session, submission)
        try:
            job, created = enqueue(
                session,
                submission,
                queue_max_depth=getattr(admission_config, capacity_field),
                origin="intake",
                principal=principal.subject,
            )
        except ValueError as exc:
            if not value_error_is_404:
                raise
            session.rollback()
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StageAtCapacityError:
            session.rollback()
            return queue_full_response(admission_config.queue_retry_after_seconds)
        session.commit()
        session.refresh(job)
        if not created:
            api_response.status_code = status.HTTP_200_OK
        return response_model.model_validate(job)

    submit_job.__doc__ = submit_doc
    router.post(
        "",
        response_model=response_model,
        status_code=status.HTTP_201_CREATED,
        responses=intake_responses
        | {
            200: {"model": response_model, "description": "Work was already enqueued"},
            404: {"description": submit_not_found_description},
        },
    )(submit_job)

    def list_jobs(
        session: Annotated[Session, Depends(session_dependency)],
        _principal: principal_annotation,
        status: Annotated[WorkUnitStatus | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Any:
        count_stmt = select(func.count()).select_from(model)
        page_stmt = select(model).order_by(model.created_at.desc())
        if status is not None:
            count_stmt = count_stmt.where(model.status == status)
            page_stmt = page_stmt.where(model.status == status)

        total = session.exec(count_stmt).one()
        jobs = session.exec(page_stmt.limit(limit).offset(offset)).all()
        return list_model(
            jobs=[response_model.model_validate(job) for job in jobs],
            total=total,
            limit=limit,
            offset=offset,
        )

    list_jobs.__doc__ = f"List {label} work-units, newest first, optionally filtered by status."
    router.get("", response_model=list_model)(list_jobs)

    def get_job(
        job_id: int,
        session: Annotated[Session, Depends(session_dependency)],
        _principal: principal_annotation,
    ) -> Any:
        return response_model.model_validate(_get_or_404(session, model, job_id, label))

    get_job.__doc__ = f"Return one {label} work-unit, or 404 if it does not exist."
    router.get("/{job_id}", response_model=response_model)(get_job)

    @router.post("/{job_id}/retry", response_model=response_model)
    def retry_job(
        job_id: int,
        session: Annotated[Session, Depends(session_dependency)],
        principal: principal_annotation,
    ) -> Any:
        """Re-queue a terminal work-unit, clear retry state, and record the event.

        Returns 409 when the status is not re-queueable.
        """
        begin_immediate_on(session)
        job = _get_or_404(session, model, job_id, label)
        try:
            retry(session, job, principal=principal.subject)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        session.refresh(job)
        return response_model.model_validate(job)

    @router.post("/{job_id}/cancel", response_model=response_model)
    def cancel_job(
        job_id: int,
        session: Annotated[Session, Depends(session_dependency)],
        _principal: principal_annotation,
    ) -> Any:
        """Cancel a work-unit and record terminal ``CANCELLED`` status.

        Returns 409 when the status is not cancellable. Running units stop
        cooperatively.
        """
        begin_immediate_on(session)
        job = _get_or_404(session, model, job_id, label)
        try:
            cancel(session, job)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        session.refresh(job)
        return response_model.model_validate(job)

    return router
