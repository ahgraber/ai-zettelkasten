"""Factory for the graph pipeline stages' console descriptors.

Contextualization and extraction are structural clones as operator surfaces — the
same work-unit table shape, the same generic ``WorkUnitStatus`` vocabulary, the
same Retry/Cancel actions, and a runs-plus-event-trail drill-down. They differ
only in their model, their ``pipeline_events`` stage, which underlying run stages
the drill-down shows, and which integer identifiers the search matches. This
factory captures those differences and returns a ready :class:`StageDescriptor`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import String, cast, func, or_
from sqlmodel import select

from fastapi import HTTPException

from aizk.console.descriptors import GRAPH_ROLLUP, StageAction, StageDescriptor
from aizk.console.monitor import (
    MonitorPage,
    clamp_limit_offset,
    execute_page,
    format_dt,
    make_page,
    resolve_direction,
)
from aizk.conversion.datamodel.source import Source
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.conversion.auth import Principal

_DEFAULT_SORT = "created_at"


def _parse_status(value: str | None) -> WorkUnitStatus | None:
    """Parse a status-filter value into a :class:`WorkUnitStatus`, or 400 on a bad value."""
    if not value:
        return None
    try:
        return WorkUnitStatus(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_status", "message": "Invalid status filter"},
        ) from exc


def build_graph_descriptor(
    *,
    key: str,
    label: str,
    model: Any,
    events_stage: str,
    drilldown_stages: list[tuple[str, str]],
    apply_retry: Callable[["Session", Any], None],
    apply_cancel: Callable[["Session", Any], None],
    id_search_columns: list[Any],
) -> StageDescriptor:
    """Build a :class:`StageDescriptor` for one graph pipeline stage.

    ``model`` is the stage's work-unit table; ``events_stage`` names its shared
    ``pipeline_events`` stage; ``drilldown_stages`` are the ``(stage, label)`` runs
    shown in the drill-down in pipeline order; ``apply_retry`` / ``apply_cancel`` are
    the stage's own transition helpers (they raise :class:`ValueError` when the unit
    is ineligible); ``id_search_columns`` are extra integer columns matched by the
    text search (beyond the id, source id, and source title).
    """
    sortable_columns: dict[str, Any] = {
        "job_id": model.id,
        "status": model.status,
        "queued_at": model.queued_at,
        "created_at": model.created_at,
    }

    def _sort_clause(sort: str | None, direction: str) -> Any:
        column = sortable_columns.get(sort, sortable_columns[_DEFAULT_SORT])
        return column.asc() if direction == "asc" else column.desc()

    def _apply_search(query: Any, search: str) -> Any:
        pattern = f"%{search.lower()}%"
        clauses = [
            func.lower(Source.title).like(pattern),
            func.lower(cast(model.source_id, String)).like(pattern),
            cast(model.id, String).like(f"%{search}%"),
        ]
        clauses.extend(cast(column, String).like(f"%{search}%") for column in id_search_columns)
        return query.where(or_(*clauses))

    def list_units(
        session: "Session",
        _principal: "Principal",
        *,
        status: str | None,
        search: str | None,
        sort: str | None,
        direction: str | None,
        limit: int,
        offset: int,
        notice: str | None = None,
    ) -> MonitorPage:
        """Query one filtered/searched/paginated page of this stage's work-units."""
        limit, offset = clamp_limit_offset(limit, offset)
        direction = resolve_direction(direction)
        status_filter = _parse_status(status)
        normalized_search = search.strip() if search else None

        base_query = select(model, Source).join(Source, Source.source_id == model.source_id)
        filtered_query = base_query
        if status_filter is not None:
            filtered_query = filtered_query.where(model.status == status_filter)
        if normalized_search:
            filtered_query = _apply_search(filtered_query, normalized_search)

        total, filtered_total, rows = execute_page(
            session, base_query, filtered_query, _sort_clause(sort, direction), limit, offset
        )

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
                    "queued_at": format_dt(job.queued_at),
                    "started_at": format_dt(job.started_at),
                    "finished_at": format_dt(job.finished_at),
                    "error_code": job.error_code or "",
                }
            )

        return make_page(
            jobs=jobs,
            total=total,
            filtered_total=filtered_total,
            limit=limit,
            offset=offset,
            status_filter=status_filter,
            search=normalized_search,
            sort=sort if sort in sortable_columns else _DEFAULT_SORT,
            direction=direction,
            notice=notice,
        )

    def count_by_status(session: "Session", _principal: "Principal") -> dict[str, int]:
        """Count this stage's work-units grouped by native status value."""
        rows = session.exec(select(model.status, func.count()).group_by(model.status)).all()
        return {status.value: count for status, count in rows}

    def get_unit(session: "Session", _principal: "Principal", unit_id: int) -> Any | None:
        """Fetch one work-unit by id (graph stages define no principal-scoping)."""
        return session.get(model, unit_id)

    def failed_split(session: "Session", _principal: "Principal") -> tuple[int, int]:
        """Split ``FAILED`` units into ``(awaiting_retry, permanent)``.

        A failed graph unit with ``earliest_next_attempt_at`` set is awaiting an
        automatic retry; one with it ``NULL`` has exhausted retries and is permanent.
        """
        failed = select(func.count()).select_from(model).where(model.status == WorkUnitStatus.FAILED)
        awaiting = session.exec(failed.where(model.earliest_next_attempt_at.is_not(None))).one()
        permanent = session.exec(failed.where(model.earliest_next_attempt_at.is_(None))).one()
        return awaiting, permanent

    def detail(session: "Session", unit: Any) -> dict[str, Any]:
        """Compose the runs section: each declared run stage, present or absent."""
        scope_id = str(unit.source_id)
        stage_names = [stage for stage, _ in drilldown_stages]
        runs = session.exec(
            select(PipelineRun)
            .where(PipelineRun.scope_id == scope_id)
            .where(PipelineRun.stage.in_(stage_names))  # type: ignore[attr-defined]
            .order_by(PipelineRun.created_at.asc())  # type: ignore[attr-defined]
        ).all()
        runs_by_stage: dict[str, list[dict[str, Any]]] = {}
        for run in runs:
            runs_by_stage.setdefault(run.stage, []).append(
                {"run_id": run.id, "status": run.status.value, "created_at": format_dt(run.created_at)}
            )
        stages = [
            {
                "stage": stage,
                "label": stage_label,
                "present": stage in runs_by_stage,
                "runs": runs_by_stage.get(stage, []),
            }
            for stage, stage_label in drilldown_stages
        ]
        return {"stages": stages}

    return StageDescriptor(
        key=key,
        label=label,
        list_units=list_units,
        count_by_status=count_by_status,
        get_unit=get_unit,
        columns_template="columns_graph.html",
        native_statuses=[status.value for status in WorkUnitStatus],
        rollup=GRAPH_ROLLUP,
        events_stage=events_stage,
        actions=[
            StageAction(
                key="retry", applied_label="retried", apply=lambda session, unit, _p: apply_retry(session, unit)
            ),
            StageAction(
                key="cancel", applied_label="cancelled", apply=lambda session, unit, _p: apply_cancel(session, unit)
            ),
        ],
        detail=detail,
        detail_template="detail_graph_runs.html",
        failed_split=failed_split,
    )
