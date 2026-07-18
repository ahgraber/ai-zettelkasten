"""Shared view-state and query helpers for the console task monitor.

A stage's ``list_units`` builds a :class:`MonitorPage` — the paginated, filtered,
searched view of its work-units — which the generic monitor template renders. The
helpers here own the parts every stage shares (limit/offset clamping, count +
page execution, pager arithmetic, and the applied-vs-skipped summary); each stage
supplies only its own model, source join, search predicate, sort mapping, and row
projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func
from sqlmodel import select

if TYPE_CHECKING:
    from sqlmodel import Session
    from sqlmodel.sql.expression import SelectOfScalar

#: Base columns every monitor row renders (checkbox, id, source_id, title, status,
#: attempts, queued/started/finished, error, actions); a stage's extra columns add
#: to this for the empty-state ``colspan``.
BASE_COLUMN_COUNT = 11
#: Upper bound on the page size, mirroring the JSON list endpoints' clamp.
MAX_LIMIT = 200


@dataclass
class MonitorPage:
    """The rendered view-state for one page of a stage's task monitor."""

    jobs: list[dict[str, Any]]
    total_jobs: int
    filtered_total: int
    limit: int
    offset: int
    start_index: int
    end_index: int
    prev_offset: int | None
    next_offset: int | None
    status_filter: Any | None
    search: str | None
    sort: str
    direction: str
    notice: str | None = None


def format_dt(value: Any) -> str:
    """Render a timestamp as an ISO-8601 string, or empty when absent."""
    if value is None:
        return ""
    return value.isoformat()


def clamp_limit_offset(limit: int, offset: int) -> tuple[int, int]:
    """Clamp the page size to ``[1, MAX_LIMIT]`` and the offset to ``>= 0``."""
    return max(1, min(limit, MAX_LIMIT)), max(offset, 0)


def resolve_direction(direction: str | None) -> str:
    """Return a valid sort direction, defaulting to ``desc``."""
    return direction if direction in {"asc", "desc"} else "desc"


def execute_page(
    session: "Session",
    base_query: "SelectOfScalar",
    filtered_query: "SelectOfScalar",
    sort_clause: Any,
    limit: int,
    offset: int,
) -> tuple[int, int, list[Any]]:
    """Return ``(total, filtered_total, rows)`` for the base and filtered queries.

    ``total`` counts the stage's whole (principal-scoped) set; ``filtered_total``
    counts the status/search-filtered set so the pager and summary report across the
    full match, not just the current page.
    """
    total = session.exec(select(func.count()).select_from(base_query.subquery())).one()
    filtered_total = session.exec(select(func.count()).select_from(filtered_query.subquery())).one()
    rows = session.exec(filtered_query.order_by(sort_clause).limit(limit).offset(offset)).all()
    return total, filtered_total, rows


def make_page(
    *,
    jobs: list[dict[str, Any]],
    total: int,
    filtered_total: int,
    limit: int,
    offset: int,
    status_filter: Any | None,
    search: str | None,
    sort: str,
    direction: str,
    notice: str | None,
) -> MonitorPage:
    """Assemble a :class:`MonitorPage` with the derived pager indices."""
    start_index = offset + 1 if filtered_total else 0
    end_index = min(offset + limit, filtered_total)
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < filtered_total else None
    return MonitorPage(
        jobs=jobs,
        total_jobs=total,
        filtered_total=filtered_total,
        limit=limit,
        offset=offset,
        start_index=start_index,
        end_index=end_index,
        prev_offset=prev_offset,
        next_offset=next_offset,
        status_filter=status_filter,
        search=search,
        sort=sort,
        direction=direction,
        notice=notice,
    )


def format_bulk_notice(applied: int, ineligible: int, not_found: int, action_label: str) -> str:
    """Summarize a bulk action: units applied, skipped as ineligible, and not found.

    An empty selection (all three counts zero) yields the informative no-op notice
    the boundary contract requires.
    """
    if applied == 0 and ineligible == 0 and not_found == 0:
        return "Select at least one work-unit."
    parts = [f"{applied} jobs {action_label}"]
    if ineligible:
        parts.append(f"{ineligible} skipped as ineligible")
    if not_found:
        parts.append(f"{not_found} not found")
    return "; ".join(parts) + "."
