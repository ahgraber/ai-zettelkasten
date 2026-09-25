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
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import String, cast, func, or_
from sqlmodel import select

from aizk.console.descriptors import GRAPH_ROLLUP, StageAction, StageDescriptor
from aizk.console.monitor import (
    MAX_LIMIT,
    MonitorPage,
    clamp_limit_offset,
    count_failed_split,
    escape_like_term,
    execute_page,
    format_dt,
    make_page,
    parse_status_filter,
    resolve_direction,
)
from aizk.conversion.datamodel.source import Source
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun

if TYPE_CHECKING:
    from uuid import UUID

    from sqlmodel import Session

    from aizk.service.auth import Principal

_DEFAULT_SORT = "created_at"


class RetryTransition(Protocol):
    """A stage's operator-retry helper: re-queues ``unit`` on behalf of ``principal``."""

    def __call__(self, session: "Session", unit: Any, *, principal: str) -> None:
        """Apply the retry inside the caller's transaction, or raise ``ValueError`` when ineligible."""


def build_graph_descriptor(
    *,
    key: str,
    label: str,
    model: Any,
    events_stage: str,
    drilldown_stages: list[tuple[str, str]],
    apply_retry: RetryTransition,
    apply_cancel: Callable[["Session", Any], None],
    id_search_columns: list[Any],
    extra_actions: list[StageAction] | None = None,
    pending_count: "Callable[[Session], int] | None" = None,
    pending_sources: "Callable[[Session, int], list[UUID]] | None" = None,
    currentness: "Callable[[Session], dict[UUID, str]] | None" = None,
) -> StageDescriptor:
    """Build a :class:`StageDescriptor` for one graph pipeline stage.

    ``model`` is the stage's work-unit table; ``events_stage`` names its shared
    ``pipeline_events`` stage; ``drilldown_stages`` are the ``(stage, label)`` runs
    shown in the drill-down in pipeline order; ``apply_retry`` / ``apply_cancel`` are
    the stage's own transition helpers (they raise :class:`ValueError` when the unit
    is ineligible; ``apply_retry`` receives the acting principal's subject as
    ``principal``); ``id_search_columns`` are extra integer columns matched by the
    text search (beyond the id, source id, and source title). ``extra_actions`` are
    actions only this stage offers, appended after the shared Retry and Cancel.

    ``pending_count`` and ``pending_sources`` are the stage's pending-work
    derivation: ``pending_count`` gives the exact backlog size and ``pending_sources``
    lists it, bounded to the console's page-size ceiling so the listing does bounded
    work whatever the backlog's size. Declare both or neither — a descriptor
    declaring only one is rejected by :class:`~aizk.console.descriptors.StageDescriptor`.
    ``currentness`` maps a stage's classified source ids to their currentness value
    (a stage-defined string; extraction's is one of ``"current"``,
    ``"needs_reconciliation"``, ``"stale"`` — see
    :class:`~aizk.extraction.core.currentness.Currentness`); a source absent from the
    mapping is unclassified. It gives the stage a stale count (sources whose value
    is ``"stale"``) and marks each monitor row's currentness, so an operator can
    distinguish and select stale and needs-reconciliation rows together. Omitting
    it leaves that console surface absent.
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
        lowered = f"%{escape_like_term(search.lower())}%"
        raw = f"%{escape_like_term(search)}%"
        clauses = [
            func.lower(Source.title).like(lowered, escape="\\"),
            func.lower(cast(model.source_id, String)).like(lowered, escape="\\"),
            cast(model.id, String).like(raw, escape="\\"),
        ]
        clauses.extend(cast(column, String).like(raw, escape="\\") for column in id_search_columns)
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
        status_filter = parse_status_filter(status)
        normalized_search = search.strip() if search else None

        # Outer join so a unit whose source_id has no ``sources`` row still lists
        # (with a source-id fallback title) and is counted the same as on the
        # dashboard's join-free ``count_by_status`` — the two never diverge.
        base_query = select(model, Source).outerjoin(Source, Source.source_id == model.source_id)
        filtered_query = base_query
        if status_filter is not None:
            filtered_query = filtered_query.where(model.status == status_filter)
        if normalized_search:
            filtered_query = _apply_search(filtered_query, normalized_search)

        total, filtered_total, rows = execute_page(
            session, base_query, filtered_query, _sort_clause(sort, direction), limit, offset
        )

        # Resolved once per page: a currentness verdict reads the source's runs,
        # so a per-row lookup would cost a query per listed unit.
        currentness_by_source = currentness(session) if currentness is not None else None

        jobs: list[dict[str, Any]] = []
        for job, source in rows:
            if job.id is None:
                continue
            job_currentness = None if currentness_by_source is None else currentness_by_source.get(job.source_id)
            jobs.append(
                {
                    "id": job.id,
                    "source_id": str(job.source_id),
                    "title": (source.title if source is not None else None) or str(job.source_id),
                    "status": job.status.value,
                    "attempts": job.attempts,
                    "queued_at": format_dt(job.queued_at),
                    "started_at": format_dt(job.started_at),
                    "finished_at": format_dt(job.finished_at),
                    "error_code": job.error_code or "",
                    "currentness": job_currentness,
                    "stale": None if currentness_by_source is None else job_currentness == "stale",
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
        return count_failed_split(session, model)

    def detail(session: "Session", unit: Any) -> dict[str, Any]:
        """Compose the runs section: each declared run stage, present or absent."""
        stage_names = [stage for stage, _ in drilldown_stages]
        runs = session.exec(
            select(PipelineRun)
            .where(PipelineRun.scope_id == unit.source_id)
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

    def _pending_count(session: "Session", _principal: "Principal") -> int:
        """Count the sources this stage owes a work-unit but has none for."""
        return pending_count(session)  # type: ignore[misc] -- only wired when declared

    def _pending_list(session: "Session", _principal: "Principal") -> list[dict[str, Any]]:
        """List the stage's pending sources, bounded to the console's page-size ceiling.

        Titles come from the ``sources`` table, falling back to the source identity,
        so a pending source reads the same way as a listed work-unit. The bound keeps
        the listing's cost from growing with the backlog; ``pending_count`` reports
        the exact size regardless.
        """
        source_ids = pending_sources(session, limit=MAX_LIMIT)  # type: ignore[misc] -- only wired when declared
        titles = {
            source.source_id: source.title
            for source in session.exec(select(Source).where(Source.source_id.in_(source_ids))).all()
        }
        return [
            {"source_id": str(source_id), "title": titles.get(source_id) or str(source_id)} for source_id in source_ids
        ]

    def stale_count(session: "Session", _principal: "Principal") -> int:
        """Count the sources whose currentness value is ``"stale"`` — not the wider re-admittable set."""
        return sum(1 for value in currentness(session).values() if value == "stale")  # type: ignore[misc]

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
                key="retry",
                applied_label="retried",
                apply=lambda session, unit, principal: apply_retry(session, unit, principal=principal.subject),
            ),
            StageAction(
                key="cancel", applied_label="cancelled", apply=lambda session, unit, _p: apply_cancel(session, unit)
            ),
            *(extra_actions or []),
        ],
        detail=detail,
        detail_template="detail_graph_runs.html",
        failed_split=failed_split,
        pending_count=_pending_count if pending_count is not None else None,
        pending_list=_pending_list if pending_sources is not None else None,
        stale_count=stale_count if currentness is not None else None,
    )
