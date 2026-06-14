"""HTML UI routes for the graph operator surface.

These routes are HTMX server-rendered like the conversion operator UI and are
mounted on the graph operator app (``graph/api/main.py``) behind its
``TrustedHostMiddleware`` perimeter. Every route resolves the request
:class:`~aizk.conversion.auth.principal.Principal` through the same
:func:`~aizk.conversion.api.dependencies.get_principal` dependency the graph JSON
API uses, so the UI is not a weaker perimeter than the API beside it. The routes
are declared ``include_in_schema=False`` where mounted, keeping HTML endpoints out
of the generated OpenAPI.

The jobs page lists contextualization work-units enriched with the source title,
supports server-side status filtering, text search, sorting, and offset
pagination, applies bulk retry/cancel by looping the per-job action helpers
behind the JSON API, and renders a per-job stage drill-down composed from
:class:`~aizk.pipeline.run.PipelineRun` stage runs and the work-unit's
:class:`~aizk.pipeline.events.PipelineEvent` lifecycle trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from sqlalchemy import String, cast, func, or_, text
from sqlmodel import Session, select

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth import Principal
from aizk.conversion.datamodel.source import Source
from aizk.graph.api.dependencies import get_db_session
from aizk.graph.api.routes import _apply_cancel, _apply_retry
from aizk.graph.contextualization import SUMMARY_STAGE, VARIANT_STAGE
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun

router = APIRouter(prefix="/ui/graph", tags=["ui"])

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))

_SORTABLE_COLUMNS: dict[str, Any] = {
    "job_id": ContextualizationJob.id,
    "status": ContextualizationJob.status,
    "queued_at": ContextualizationJob.queued_at,
    "created_at": ContextualizationJob.created_at,
}
_DEFAULT_SORT = "created_at"
_DEFAULT_DIRECTION = "desc"

#: Graph stages whose runs are shown in the per-job drill-down, in pipeline order.
_DRILLDOWN_STAGES: list[tuple[str, str]] = [
    (CHUNKING_STAGE, "Chunking"),
    (SUMMARY_STAGE, "Document Summary"),
    (VARIANT_STAGE, "Chunk Contextualization"),
]


@dataclass
class JobsPage:
    """Represents the UI state for the contextualization jobs list."""

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

    Text search matches the job identifier, the source ``aizk_uuid``, the enriched
    source title, and the ``conversion_output`` identifier.
    """
    if status_filter:
        query = query.where(ContextualizationJob.status == status_filter)
    if search:
        lowered = search.lower()
        pattern = f"%{lowered}%"
        query = query.where(
            or_(
                func.lower(Source.title).like(pattern),
                func.lower(cast(ContextualizationJob.aizk_uuid, String)).like(pattern),
                cast(ContextualizationJob.id, String).like(f"%{search}%"),
                cast(ContextualizationJob.conversion_output_id, String).like(f"%{search}%"),
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
) -> JobsPage:
    """Query one page of work-units joined to their source and build the view state."""
    limit = max(1, min(limit, 200))
    offset = max(offset, 0)
    sort_key = _SORTABLE_COLUMNS[_to_sort(sort)]
    sort_clause = sort_key.asc() if _to_direction(direction) == "asc" else sort_key.desc()

    base_query = select(ContextualizationJob, Source).join(Source, Source.aizk_uuid == ContextualizationJob.aizk_uuid)
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
                "conversion_output_id": job.conversion_output_id,
                "title": source.title or str(job.aizk_uuid),
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


def _page_context(page: JobsPage) -> dict[str, Any]:
    """Build the template context for the jobs panel, including status filter options."""
    return {"page": page, "status_options": [status.value for status in WorkUnitStatus]}


@router.get("/jobs")
def graph_ui_jobs(
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
    """Render the contextualization jobs page (full page or ``HX-Request`` partial)."""
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
    return _TEMPLATES.TemplateResponse(request, template, _page_context(page))


def _format_bulk_notice(applied: int, ineligible: int, action_label: str, selected_ids: list[int]) -> str:
    """Summarize a bulk action: jobs applied and jobs skipped as ineligible."""
    if not selected_ids:
        return "Select at least one job."
    parts: list[str] = [f"{applied} jobs {action_label}"]
    if ineligible:
        parts.append(f"{ineligible} skipped as ineligible")
    return "; ".join(parts) + "."


@router.post("/jobs/actions")
def graph_ui_job_actions(
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
        job = session.get(ContextualizationJob, job_id)
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
    return _TEMPLATES.TemplateResponse(request, "jobs_panel.html", _page_context(page))


def _load_stage_drilldown(session: Session, job: ContextualizationJob) -> dict[str, Any]:
    """Compose the per-job stage drill-down: stage runs plus the work-unit event trail.

    Stage runs come from :class:`~aizk.pipeline.run.PipelineRun` for the source's
    ``scope_key`` (``str(aizk_uuid)``) across the chunking, document-summary, and
    chunk-contextualization stages; a stage with no run renders as absent. The event
    trail is the work-unit's :class:`~aizk.pipeline.events.PipelineEvent` lifecycle
    rows under the contextualization stage, keyed by the work-unit reference and
    ordered chronologically.
    """
    scope_key = str(job.aizk_uuid)
    stage_names = [stage for stage, _ in _DRILLDOWN_STAGES]
    runs = session.exec(
        select(PipelineRun)
        .where(PipelineRun.scope_key == scope_key)
        .where(PipelineRun.stage.in_(stage_names))  # type: ignore[attr-defined]
        .order_by(PipelineRun.created_at.asc())  # type: ignore[attr-defined]
    ).all()
    runs_by_stage: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        runs_by_stage.setdefault(run.stage, []).append(
            {"run_id": run.id, "status": run.status.value, "created_at": _format_dt(run.created_at)}
        )

    stages = [
        {
            "stage": stage,
            "label": label,
            "present": stage in runs_by_stage,
            "runs": runs_by_stage.get(stage, []),
        }
        for stage, label in _DRILLDOWN_STAGES
    ]

    events = session.exec(
        select(PipelineEvent)
        .where(PipelineEvent.stage == CONTEXTUALIZATION_STAGE)
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
        "aizk_uuid": scope_key,
        "stages": stages,
        "events": event_views,
    }


@router.get("/jobs/{job_id}/stages")
def graph_ui_job_stages(
    request: Request,
    job_id: int,
    session: Annotated[Session, Depends(get_db_session)],
    _principal: Annotated[Principal, Depends(get_principal)],
):
    """Render the per-job stage drill-down partial, or 404 if the work-unit is unknown."""
    job = session.get(ContextualizationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"contextualization work-unit {job_id} not found")
    drilldown = _load_stage_drilldown(session, job)
    return _TEMPLATES.TemplateResponse(request, "job_stages.html", {"drilldown": drilldown})
