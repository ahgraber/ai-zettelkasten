"""HTML UI routes for conversion jobs."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from pathlib import Path
from typing import Annotated, Any

from sqlalchemy import String, cast, func, or_, text
from sqlmodel import Session, select

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates

from aizk.conversion.api.dependencies import get_db_session
from aizk.conversion.api.routes.jobs import _apply_job_cancel, _apply_job_delete, _apply_job_retry
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source

router = APIRouter(prefix="/ui", tags=["ui"])

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))

_SORTABLE_COLUMNS: dict[str, Any] = {
    "job_id": ConversionJob.id,
    "status": ConversionJob.status,
    "queued_at": ConversionJob.queued_at,
    "created_at": ConversionJob.created_at,
}
_DEFAULT_SORT = "queued_at"
_DEFAULT_DIRECTION = "desc"


@dataclass
class JobsPage:
    """Represents the UI state for the jobs list."""

    jobs: list[dict[str, Any]]
    total_jobs: int
    filtered_total: int
    limit: int
    offset: int
    start_index: int
    end_index: int
    prev_offset: int | None
    next_offset: int | None
    status_filter: ConversionJobStatus | None
    search: str | None
    sort: str
    direction: str
    notice: str | None = None


def _format_dt(value) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _to_sort(sort: str | None) -> str:
    if sort in _SORTABLE_COLUMNS:
        return sort
    return _DEFAULT_SORT


def _to_direction(direction: str | None) -> str:
    if direction in {"asc", "desc"}:
        return direction
    return _DEFAULT_DIRECTION


def _parse_status_filter(value: str | None) -> ConversionJobStatus | None:
    if not value:
        return None
    try:
        return ConversionJobStatus(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_status", "message": "Invalid status filter"},
        ) from exc


def _apply_filters(
    query,
    status_filter: ConversionJobStatus | None,
    search: str | None,
) -> Any:
    if status_filter:
        query = query.where(ConversionJob.status == status_filter)
    if search:
        lowered = search.lower()
        pattern = f"%{lowered}%"
        query = query.where(
            or_(
                func.lower(ConversionJob.title).like(pattern),
                func.lower(Source.karakeep_id).like(pattern),
                func.lower(cast(ConversionJob.aizk_uuid, String)).like(pattern),
                cast(ConversionJob.id, String).like(f"%{search}%"),
            )
        )
    return query


def _load_jobs_page(
    session: Session,
    limit: int,
    offset: int,
    status_filter: ConversionJobStatus | None,
    search: str | None,
    sort: str,
    direction: str,
    notice: str | None,
) -> JobsPage:
    limit = max(1, min(limit, 200))
    offset = max(offset, 0)
    sort_key = _SORTABLE_COLUMNS[_to_sort(sort)]
    sort_clause = sort_key.asc() if _to_direction(direction) == "asc" else sort_key.desc()

    base_query = select(ConversionJob, Source).join(Source, Source.aizk_uuid == ConversionJob.aizk_uuid)
    total_jobs = session.exec(select(func.count()).select_from(base_query.subquery())).one()

    filtered_query = _apply_filters(base_query, status_filter, search)
    filtered_total = session.exec(select(func.count()).select_from(filtered_query.subquery())).one()

    rows = session.exec(filtered_query.order_by(sort_clause).limit(limit).offset(offset)).all()

    jobs: list[dict[str, Any]] = []
    for job, source in rows:
        if job.id is None:
            continue
        jobs.append(
            {
                "id": job.id,
                "aizk_uuid": str(job.aizk_uuid),
                "karakeep_id": source.karakeep_id,
                "title": job.title or source.title or "",
                "status": job.status.value,
                "attempts": job.attempts,
                "queued_at": _format_dt(job.queued_at),
                "started_at": _format_dt(job.started_at),
                "finished_at": _format_dt(job.finished_at),
                "error_code": job.error_code or "",
            }
        )

    start_index = offset + 1 if filtered_total else 0
    end_index = min(offset + limit, filtered_total)
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < filtered_total else None

    return JobsPage(
        jobs=jobs,
        total_jobs=total_jobs,
        filtered_total=filtered_total,
        limit=limit,
        offset=offset,
        start_index=start_index,
        end_index=end_index,
        prev_offset=prev_offset,
        next_offset=next_offset,
        status_filter=status_filter,
        search=search,
        sort=_to_sort(sort),
        direction=_to_direction(direction),
        notice=notice,
    )


@router.get("/jobs")
def ui_jobs(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    sort: Annotated[str | None, Query()] = _DEFAULT_SORT,
    direction: Annotated[str | None, Query()] = _DEFAULT_DIRECTION,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Render the jobs Web UI."""
    normalized_search = search.strip() if search else None
    page = _load_jobs_page(
        session=session,
        limit=limit,
        offset=offset,
        status_filter=_parse_status_filter(status_filter),
        search=normalized_search,
        sort=sort,
        direction=direction,
        notice=None,
    )

    template = "jobs_panel.html" if request.headers.get("HX-Request") else "jobs.html"
    return _TEMPLATES.TemplateResponse(request, template, {"page": page})


def _format_bulk_notice(
    applied: int,
    ineligible: int,
    action_label: str,
    selected_ids: list[int],
) -> str:
    if not selected_ids:
        return "Select at least one job."
    parts: list[str] = [f"{applied} jobs {action_label}"]
    if ineligible:
        parts.append(f"{ineligible} skipped as ineligible")
    return "; ".join(parts) + "."


@router.post("/jobs/actions")
def ui_job_actions(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    action: Annotated[str, Form()],
    job_ids: Annotated[list[int] | None, Form()] = None,
    status_filter: Annotated[str | None, Form(alias="status")] = None,
    search: Annotated[str | None, Form()] = None,
    sort: Annotated[str | None, Form()] = _DEFAULT_SORT,
    direction: Annotated[str | None, Form()] = _DEFAULT_DIRECTION,
    limit: Annotated[int, Form(ge=1, le=200)] = 50,
    offset: Annotated[int, Form(ge=0)] = 0,
):
    """Apply retry, cancel, or delete actions from the Web UI."""
    session.exec(text("BEGIN IMMEDIATE"))
    if action not in {"retry", "cancel", "delete"}:
        raise HTTPException(status_code=400, detail={"error": "invalid_action", "message": "Invalid action"})

    now = dt.datetime.now(dt.timezone.utc)
    selected_ids = job_ids or []
    applied = 0
    ineligible = 0

    for job_id in selected_ids:
        job = session.get(ConversionJob, job_id)
        if not job:
            ineligible += 1
            continue
        try:
            if action == "retry":
                _apply_job_retry(job, now)
                session.add(job)
            elif action == "cancel":
                _apply_job_cancel(job, now)
                session.add(job)
            else:
                _apply_job_delete(session, job)
            applied += 1
        except ValueError:
            ineligible += 1

    session.commit()

    action_label = {"retry": "retried", "cancel": "cancelled", "delete": "deleted"}[action]
    notice = _format_bulk_notice(applied, ineligible, action_label, selected_ids)
    normalized_search = search.strip() if search else None
    page = _load_jobs_page(
        session=session,
        limit=limit,
        offset=offset,
        status_filter=_parse_status_filter(status_filter),
        search=normalized_search,
        sort=sort,
        direction=direction,
        notice=notice,
    )

    return _TEMPLATES.TemplateResponse(
        request,
        "jobs_panel.html",
        {"page": page},
    )
