"""Generic, descriptor-driven console routes: task monitor, drill-down, actions.

One set of routes renders any registered stage. The monitor lists a stage's
work-units with server-side filter/search/sort/pagination; the drill-down shows
the unit's lifecycle event trail (from the shared ``pipeline_events``) plus the
stage's optional runs/artifact detail; the actions route applies a stage's
declared bulk actions with skip-and-report semantics. Every route resolves the
same request :class:`~aizk.conversion.auth.Principal` the JSON APIs require and
sits behind the app's trusted-host perimeter, so the console is no weaker a
perimeter than the APIs beside it.
"""

from __future__ import annotations

import importlib.resources
from typing import TYPE_CHECKING, Annotated, Any

from sqlalchemy import text
from sqlmodel import Session, select

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates

from aizk.console.descriptors import StageDescriptor, get_descriptor, registered_stages
from aizk.console.monitor import BASE_COLUMN_COUNT, format_bulk_notice, format_dt
import aizk.console.stages  # noqa: F401 -- import registers the stage descriptors
from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth import Principal
from aizk.graph.api.dependencies import get_db_session
from aizk.pipeline.events import PipelineEvent

if TYPE_CHECKING:
    from aizk.console.monitor import MonitorPage

#: Maximum work-units a single bulk action may target, keeping the write
#: transaction bounded (mirrors the JSON bulk endpoint's ``1..100`` bound).
MAX_BULK_SELECTION = 100

router = APIRouter(prefix="/ui/tasks", tags=["console"])

#: The console's own templates, with the graph package's shell partials
#: (``_nav.html`` / ``_styles_*``) resolvable as a fallback.
_TEMPLATES = Jinja2Templates(
    directory=[
        str(importlib.resources.files("aizk.console") / "templates"),
        str(importlib.resources.files("aizk.graph") / "templates"),
    ]
)


def _require_descriptor(stage: str | None) -> StageDescriptor:
    """Resolve the descriptor for ``stage``, defaulting to the first registered one.

    An explicit but unregistered stage key is a not-found; this runs before any
    stage data is touched.
    """
    if stage is None:
        stages = registered_stages()
        if not stages:
            raise HTTPException(status_code=404, detail="no stages registered")
        return stages[0]
    descriptor = get_descriptor(stage)
    if descriptor is None:
        raise HTTPException(status_code=404, detail=f"unknown stage {stage!r}")
    return descriptor


def _monitor_context(descriptor: StageDescriptor, page: "MonitorPage") -> dict[str, Any]:
    """Build the template context shared by the full page and the panel partial."""
    return {
        "descriptor": descriptor,
        "page": page,
        "colspan": BASE_COLUMN_COUNT + descriptor.extra_columns,
    }


@router.get("")
def monitor(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    _principal: Annotated[Principal, Depends(get_principal)],
    stage: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    sort: Annotated[str | None, Query()] = None,
    direction: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Render a stage's task monitor (full page, or the panel partial on ``HX-Request``)."""
    descriptor = _require_descriptor(stage)
    page = descriptor.list_units(
        session,
        _principal,
        status=status_filter,
        search=search,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
    )
    template = "tasks_panel.html" if request.headers.get("HX-Request") else "tasks.html"
    return _TEMPLATES.TemplateResponse(request, template, _monitor_context(descriptor, page))


@router.post("/{stage}/actions")
def actions(
    request: Request,
    stage: str,
    session: Annotated[Session, Depends(get_db_session)],
    principal: Annotated[Principal, Depends(get_principal)],
    action: Annotated[str, Form()],
    job_ids: Annotated[list[int] | None, Form()] = None,
    status_filter: Annotated[str | None, Form(alias="status")] = None,
    search: Annotated[str | None, Form()] = None,
    sort: Annotated[str | None, Form()] = None,
    direction: Annotated[str | None, Form()] = None,
    limit: Annotated[int, Form(ge=1, le=200)] = 50,
    offset: Annotated[int, Form(ge=0)] = 0,
):
    """Apply a stage's declared bulk action over the selected units, skip-and-report.

    Boundary checks run before any unit is touched: an unregistered stage is
    not-found, an undeclared action or a malformed status filter is rejected, and a
    selection above the cap is rejected whole. A non-empty selection is applied in
    one ``BEGIN IMMEDIATE`` transaction; a missing unit is reported not-found and an
    ineligible unit (the helper raises :class:`ValueError`) is skipped, both leaving
    status unchanged. An empty selection is a no-op that takes no write lock.
    """
    descriptor = _require_descriptor(stage)
    declared = {declared_action.key: declared_action for declared_action in descriptor.actions}
    if action not in declared:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_action", "message": f"Action {action!r} is not declared for this stage"},
        )
    selected_ids = job_ids or []
    if len(selected_ids) > MAX_BULK_SELECTION:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "selection_too_large",
                "message": f"Select at most {MAX_BULK_SELECTION} work-units",
            },
        )
    # Validate the carried status filter here too, before any mutation, so a
    # malformed value rejects the whole action rather than committing it and then
    # failing on the re-render.
    if status_filter and status_filter not in descriptor.native_statuses:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_status", "message": "Invalid status filter"},
        )

    declared_action = declared[action]
    applied = ineligible = not_found = 0
    # Only the mutating path takes the write lock; an empty selection is a no-op.
    if selected_ids:
        session.exec(text("BEGIN IMMEDIATE"))
        for unit_id in selected_ids:
            unit = descriptor.get_unit(session, principal, unit_id)
            if unit is None:
                not_found += 1
                continue
            try:
                declared_action.apply(session, unit, principal)
                applied += 1
            except ValueError:
                ineligible += 1
        session.commit()

    notice = format_bulk_notice(applied, ineligible, not_found, declared_action.applied_label)
    page = descriptor.list_units(
        session,
        principal,
        status=status_filter,
        search=search,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
        notice=notice,
    )
    return _TEMPLATES.TemplateResponse(request, "tasks_panel.html", _monitor_context(descriptor, page))


def _event_trail(session: Session, events_stage: str, unit_id: int) -> list[dict[str, Any]]:
    """Read a unit's lifecycle event trail from the shared event log, chronologically."""
    events = session.exec(
        select(PipelineEvent)
        .where(PipelineEvent.stage == events_stage)
        .where(PipelineEvent.work_unit_ref == str(unit_id))
        .order_by(PipelineEvent.occurred_at.asc(), PipelineEvent.event_id.asc())  # type: ignore[attr-defined]
    ).all()
    return [
        {
            "kind": event.kind,
            "from_status": event.from_status or "",
            "to_status": event.to_status or "",
            "attempt": event.attempt,
            "occurred_at": format_dt(event.occurred_at),
        }
        for event in events
    ]


@router.get("/{stage}/{unit_id}")
def drilldown(
    request: Request,
    stage: str,
    unit_id: int,
    session: Annotated[Session, Depends(get_db_session)],
    principal: Annotated[Principal, Depends(get_principal)],
):
    """Render a unit's drill-down: the stage's detail section (if any) plus the event trail."""
    descriptor = _require_descriptor(stage)
    unit = descriptor.get_unit(session, principal, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail=f"{descriptor.key} work-unit {unit_id} not found")
    detail = descriptor.detail(session, unit) if descriptor.detail is not None else None
    drilldown_view = {
        "unit_id": unit_id,
        "source_id": str(unit.source_id),
        "detail": detail,
        "events": _event_trail(session, descriptor.events_stage, unit_id),
    }
    return _TEMPLATES.TemplateResponse(
        request, "task_detail.html", {"descriptor": descriptor, "drilldown": drilldown_view}
    )
