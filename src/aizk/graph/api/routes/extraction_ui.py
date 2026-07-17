"""HTML UI routes for the extraction stage's operator surface.

Mirrors ``aizk.graph.api.routes.ui`` (the contextualization jobs page) in route
structure, template shape, and behavior — the list/filter/search/sort/paginate
table, bulk retry/cancel, and the per-job drill-down composed from the
extraction run and the work-unit's :class:`~aizk.pipeline.events.PipelineEvent`
lifecycle trail — parameterized for
:class:`~aizk.graph.datamodel.ExtractionJob`. It deliberately does **not**
mirror the contextualization UI's content explorer/search surface (raw vs.
contextualized chunk browsing): that is a chunk-content browsing feature
specific to contextualization's own artifact, not part of the design's
"work-unit list/detail/retry/cancel over the runtime's event/run records"
operator-view contract this stage owns.

These routes are HTMX server-rendered like the contextualization jobs page and
are mounted on the graph operator app (``graph/api/main.py``) behind its
``TrustedHostMiddleware`` perimeter, resolving the same
:class:`~aizk.conversion.auth.principal.Principal` the extraction JSON API uses.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.resources
from typing import Annotated, Any

from sqlalchemy import String, cast, func, or_, text
from sqlmodel import Session, select

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth import Principal
from aizk.conversion.datamodel.source import Source
from aizk.graph.api.dependencies import get_db_session
from aizk.graph.api.routes.extraction import _apply_cancel, _apply_retry
from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun

router = APIRouter(prefix="/ui/graph", tags=["ui"])

_TEMPLATES = Jinja2Templates(directory=str(importlib.resources.files("aizk.graph") / "templates"))

_SORTABLE_COLUMNS: dict[str, Any] = {
    "job_id": ExtractionJob.id,
    "status": ExtractionJob.status,
    "queued_at": ExtractionJob.queued_at,
    "created_at": ExtractionJob.created_at,
}
_DEFAULT_SORT = "created_at"
_DEFAULT_DIRECTION = "desc"

#: The extraction run's own drill-down label. Unlike contextualization (whose
#: work-unit stage spans three underlying run stages), extraction's work-unit
#: stage coincides with exactly one underlying run stage.
_DRILLDOWN_STAGES: list[tuple[str, str]] = [(EXTRACTION_STAGE, "Mention Extraction")]


@dataclass
class ExtractionJobsPage:
    """Represents the UI state for the extraction jobs list."""

    jobs: list[dict[str, Any]]
    total_jobs: int
    filtered_total: int
    limit: int
    offset: int
    start_index: int
    end_index: int
    prev_offset: int | None
    next_offset: int | None
    status_filter: WorkUnitStatus | None
    search: str | None
    sort: str
    direction: str
    notice: str | None = None


def _format_dt(value) -> str:
    """Render a timestamp as an ISO-8601 string, or empty when absent."""
    if value is None:
        return ""
    return value.isoformat()


def _to_sort(sort: str | None) -> str:
    """Return a known sort key, falling back to the default."""
    if sort in _SORTABLE_COLUMNS:
        return sort
    return _DEFAULT_SORT


def _to_direction(direction: str | None) -> str:
    """Return a valid sort direction, falling back to the default."""
    if direction in {"asc", "desc"}:
        return direction
    return _DEFAULT_DIRECTION


def _parse_status_filter(value: str | None) -> WorkUnitStatus | None:
    """Parse the status filter value into a :class:`WorkUnitStatus`, or 400 on a bad value."""
    if not value:
        return None
    try:
        return WorkUnitStatus(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_status", "message": "Invalid status filter"},
        ) from exc


def _apply_filters(query, status_filter: WorkUnitStatus | None, search: str | None) -> Any:
    """Apply the status filter and free-text search across the full job set.

    Text search matches the job identifier, the source ``source_id``, and the
    enriched source title.
    """
    if status_filter:
        query = query.where(ExtractionJob.status == status_filter)
    if search:
        lowered = search.lower()
        pattern = f"%{lowered}%"
        query = query.where(
            or_(
                func.lower(Source.title).like(pattern),
                func.lower(cast(ExtractionJob.source_id, String)).like(pattern),
                cast(ExtractionJob.id, String).like(f"%{search}%"),
            )
        )
    return query


def _load_jobs_page(
    session: Session,
    limit: int,
    offset: int,
    status_filter: WorkUnitStatus | None,
    search: str | None,
    sort: str,
    direction: str,
    notice: str | None,
) -> ExtractionJobsPage:
    """Query one page of work-units joined to their source and build the view state."""
    limit = max(1, min(limit, 200))
    offset = max(offset, 0)
    sort_key = _SORTABLE_COLUMNS[_to_sort(sort)]
    sort_clause = sort_key.asc() if _to_direction(direction) == "asc" else sort_key.desc()

    base_query = select(ExtractionJob, Source).join(Source, Source.source_id == ExtractionJob.source_id)
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
                "source_id": str(job.source_id),
                "title": source.title or str(job.source_id),
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

    return ExtractionJobsPage(
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


def _page_context(page: ExtractionJobsPage) -> dict[str, Any]:
    """Build the template context for the jobs panel, including status filter options."""
    return {"page": page, "status_options": [status.value for status in WorkUnitStatus]}


@router.get("/extraction-jobs")
def graph_ui_extraction_jobs(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    _principal: Annotated[Principal, Depends(get_principal)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    sort: Annotated[str | None, Query()] = _DEFAULT_SORT,
    direction: Annotated[str | None, Query()] = _DEFAULT_DIRECTION,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Render the extraction jobs page (full page or ``HX-Request`` partial)."""
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
    template = "extraction_jobs_panel.html" if request.headers.get("HX-Request") else "extraction_jobs.html"
    return _TEMPLATES.TemplateResponse(request, template, _page_context(page))


def _format_bulk_notice(applied: int, ineligible: int, action_label: str, selected_ids: list[int]) -> str:
    """Summarize a bulk action: jobs applied and jobs skipped as ineligible."""
    if not selected_ids:
        return "Select at least one job."
    parts: list[str] = [f"{applied} jobs {action_label}"]
    if ineligible:
        parts.append(f"{ineligible} skipped as ineligible")
    return "; ".join(parts) + "."


@router.post("/extraction-jobs/actions")
def graph_ui_extraction_job_actions(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    _principal: Annotated[Principal, Depends(get_principal)],
    action: Annotated[str, Form()],
    job_ids: Annotated[list[int] | None, Form()] = None,
    status_filter: Annotated[str | None, Form(alias="status")] = None,
    search: Annotated[str | None, Form()] = None,
    sort: Annotated[str | None, Form()] = _DEFAULT_SORT,
    direction: Annotated[str | None, Form()] = _DEFAULT_DIRECTION,
    limit: Annotated[int, Form(ge=1, le=200)] = 50,
    offset: Annotated[int, Form(ge=0)] = 0,
):
    """Apply a bulk retry or cancel over the selected work-units.

    Loops the shared per-job action helpers behind the JSON API, each in the same
    transaction, accumulating an applied-vs-skipped summary. A missing job or one
    ineligible for the action (the helper raises :class:`ValueError`) is counted as
    skipped and its status is left unchanged.
    """
    session.exec(text("BEGIN IMMEDIATE"))
    if action not in {"retry", "cancel"}:
        raise HTTPException(status_code=400, detail={"error": "invalid_action", "message": "Invalid action"})

    selected_ids = job_ids or []
    applied = 0
    ineligible = 0
    for job_id in selected_ids:
        job = session.get(ExtractionJob, job_id)
        if job is None:
            ineligible += 1
            continue
        try:
            if action == "retry":
                _apply_retry(session, job)
            else:
                _apply_cancel(session, job)
            applied += 1
        except ValueError:
            ineligible += 1

    session.commit()

    action_label = {"retry": "retried", "cancel": "cancelled"}[action]
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
    return _TEMPLATES.TemplateResponse(request, "extraction_jobs_panel.html", _page_context(page))


def _load_stage_drilldown(session: Session, job: ExtractionJob) -> dict[str, Any]:
    """Compose the per-job drill-down: the extraction run plus the work-unit event trail.

    The extraction run comes from :class:`~aizk.pipeline.run.PipelineRun` for
    the source's ``scope_id`` (``str(source_id)``) under the
    :data:`~aizk.graph.extraction_events.EXTRACTION_STAGE` run stage; absent when
    the source has never had a successful extraction run. The event trail is
    the work-unit's :class:`~aizk.pipeline.events.PipelineEvent` lifecycle rows,
    keyed by the work-unit reference and ordered chronologically.
    """
    scope_id = str(job.source_id)
    runs = session.exec(
        select(PipelineRun)
        .where(PipelineRun.scope_id == scope_id, PipelineRun.stage == EXTRACTION_STAGE)
        .order_by(PipelineRun.created_at.asc())  # type: ignore[attr-defined]
    ).all()
    run_views = [
        {"run_id": run.id, "status": run.status.value, "created_at": _format_dt(run.created_at)} for run in runs
    ]
    stages = [
        {"stage": stage, "label": label, "present": bool(run_views), "runs": run_views}
        for stage, label in _DRILLDOWN_STAGES
    ]

    events = session.exec(
        select(PipelineEvent)
        .where(PipelineEvent.stage == EXTRACTION_STAGE)
        .where(PipelineEvent.work_unit_ref == str(job.id))
        .order_by(PipelineEvent.occurred_at.asc(), PipelineEvent.event_id.asc())  # type: ignore[attr-defined]
    ).all()
    event_views = [
        {
            "kind": event.kind,
            "from_status": event.from_status or "",
            "to_status": event.to_status or "",
            "attempt": event.attempt,
            "occurred_at": _format_dt(event.occurred_at),
        }
        for event in events
    ]

    return {
        "job_id": job.id,
        "source_id": scope_id,
        "stages": stages,
        "events": event_views,
    }


@router.get("/extraction-jobs/{job_id}/stages")
def graph_ui_extraction_job_stages(
    request: Request,
    job_id: int,
    session: Annotated[Session, Depends(get_db_session)],
    _principal: Annotated[Principal, Depends(get_principal)],
):
    """Render the per-job drill-down partial, or 404 if the work-unit is unknown."""
    job = session.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"extraction work-unit {job_id} not found")
    drilldown = _load_stage_drilldown(session, job)
    return _TEMPLATES.TemplateResponse(request, "extraction_job_stages.html", {"drilldown": drilldown})
