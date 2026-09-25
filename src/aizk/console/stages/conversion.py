"""Console descriptor for the conversion stage.

Conversion differs from the graph stages as an operator surface, and the
descriptor captures exactly those differences: its work-units are owner-scoped
(every list, count, and fetch is filtered to the request principal's subject,
mirroring the conversion JSON API), it carries the KaraKeep bookmark id as an
extra column and a searchable identifier, it declares a Delete action beyond
Retry and Cancel, and its drill-down shows the produced
:class:`~aizk.conversion.datamodel.output.ConversionOutput`. Retry, cancel, and
delete dispatch to the :mod:`aizk.conversion.stage` and
:mod:`aizk.conversion.job_actions` domain helpers — the same code the JSON API
calls — so the console is a second caller of the domain, not a second
implementation.

The module imports only conversion *domain* code (datamodel, stage, and
job-actions); the conversion app's route, wiring, and processing modules stay
off-limits so the console never couples to another app's internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import String, cast, func, or_
from sqlmodel import select

from aizk.console.descriptors import StageAction, StageDescriptor
from aizk.console.monitor import (
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
from aizk.conversion.datamodel.events import STAGE as CONVERSION_STAGE
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.job_actions import apply_job_delete
from aizk.conversion.stage import cancel_conversion_job, retry_conversion_job
from aizk.pipeline.lifecycle import WorkUnitStatus, derive_retry_class

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.service.auth import Principal

#: Conversion persists :class:`WorkUnitStatus`, so its rollup is the identity map;
#: the dashboard subdivides failed by repairability (via ``failed_split``).
CONVERSION_ROLLUP: dict[WorkUnitStatus, WorkUnitStatus] = {status: status for status in WorkUnitStatus}

_DEFAULT_SORT = "queued_at"
_SORTABLE_COLUMNS: dict[str, Any] = {
    "job_id": ConversionJob.id,
    "status": ConversionJob.status,
    "queued_at": ConversionJob.queued_at,
    "created_at": ConversionJob.created_at,
}


def _sort_clause(sort: str | None, direction: str) -> Any:
    """Resolve the ORDER BY clause for a (possibly unknown) sort key and direction."""
    column = _SORTABLE_COLUMNS.get(sort, _SORTABLE_COLUMNS[_DEFAULT_SORT])
    return column.asc() if direction == "asc" else column.desc()


def _apply_search(query: Any, search: str) -> Any:
    """Match the search across the job title, source title, KaraKeep id, source id, and job id."""
    lowered = f"%{escape_like_term(search.lower())}%"
    raw = f"%{escape_like_term(search)}%"
    return query.where(
        or_(
            func.lower(ConversionJob.title).like(lowered, escape="\\"),
            func.lower(Source.title).like(lowered, escape="\\"),
            func.lower(Source.karakeep_id).like(lowered, escape="\\"),
            func.lower(cast(ConversionJob.source_id, String)).like(lowered, escape="\\"),
            cast(ConversionJob.id, String).like(raw, escape="\\"),
        )
    )


def list_units(
    session: "Session",
    principal: "Principal",
    *,
    status: str | None,
    search: str | None,
    sort: str | None,
    direction: str | None,
    limit: int,
    offset: int,
    notice: str | None = None,
) -> MonitorPage:
    """Query one owner-scoped, filtered/searched/paginated page of conversion jobs."""
    limit, offset = clamp_limit_offset(limit, offset)
    direction = resolve_direction(direction)
    status_filter = parse_status_filter(status)
    normalized_search = search.strip() if search else None

    # Outer join so a job whose source_id has no ``sources`` row still lists (with a
    # submit-time-title fallback) and is counted the same as on the dashboard's
    # join-free ``count_by_status`` — the monitor total and dashboard total never diverge.
    base_query = (
        select(ConversionJob, Source)
        .outerjoin(Source, Source.source_id == ConversionJob.source_id)
        .where(ConversionJob.owner_id == principal.subject)
    )
    filtered_query = base_query
    if status_filter is not None:
        filtered_query = filtered_query.where(ConversionJob.status == status_filter)
    if normalized_search:
        filtered_query = _apply_search(filtered_query, normalized_search)

    total, filtered_total, rows = execute_page(
        session, base_query, filtered_query, _sort_clause(sort, direction), limit, offset
    )

    jobs: list[dict[str, Any]] = []
    for job, source in rows:
        if job.id is None:
            continue
        retry_class = derive_retry_class(job.status, job.earliest_next_attempt_at)
        jobs.append(
            {
                "id": job.id,
                "source_id": str(job.source_id),
                "karakeep_id": (source.karakeep_id if source is not None else None) or "",
                # Enriched source title, falling back to the submit-time job title.
                "title": (source.title if source is not None else None) or job.title or "",
                "status": job.status.value,
                "phase": (job.phase if job.status is WorkUnitStatus.RUNNING else None) or "",
                "retry_class": retry_class.value if retry_class is not None else "",
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
        sort=sort if sort in _SORTABLE_COLUMNS else _DEFAULT_SORT,
        direction=direction,
        notice=notice,
    )


def count_by_status(session: "Session", principal: "Principal") -> dict[str, int]:
    """Count the principal's conversion jobs grouped by native status value."""
    rows = session.exec(
        select(ConversionJob.status, func.count())
        .where(ConversionJob.owner_id == principal.subject)
        .group_by(ConversionJob.status)
    ).all()
    return {status.value: count for status, count in rows}


def get_unit(session: "Session", principal: "Principal", unit_id: int) -> ConversionJob | None:
    """Fetch one conversion job by id, honoring owner-scoping (else ``None``)."""
    job = session.get(ConversionJob, unit_id)
    if job is None or job.owner_id != principal.subject:
        return None
    return job


def failed_split(session: "Session", principal: "Principal") -> tuple[int, int]:
    """Split ``FAILED`` conversion jobs into ``(awaiting_retry, permanent)``.

    A failed job with ``earliest_next_attempt_at`` set is awaiting an automatic
    retry; one with it ``NULL`` is permanent.
    """
    return count_failed_split(session, ConversionJob, ConversionJob.owner_id == principal.subject)


def detail(session: "Session", unit: ConversionJob) -> dict[str, Any]:
    """Compose the drill-down detail: the job's produced conversion output, if any."""
    output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == unit.id)).first()
    if output is None:
        return {"output": None}
    return {
        "output": {
            "id": output.id,
            "title": output.title,
            "s3_prefix": output.s3_prefix,
            "markdown_key": output.markdown_key,
            "markdown_hash_xx64": output.markdown_hash_xx64,
            "figure_count": output.figure_count,
            "docling_version": output.docling_version,
            "pipeline_name": output.pipeline_name,
            "created_at": format_dt(output.created_at),
        }
    }


DESCRIPTOR = StageDescriptor(
    key="conversion",
    label="Conversion",
    list_units=list_units,
    count_by_status=count_by_status,
    get_unit=get_unit,
    columns_template="columns_conversion.html",
    native_statuses=[status.value for status in WorkUnitStatus],
    rollup=CONVERSION_ROLLUP,
    events_stage=CONVERSION_STAGE,
    actions=[
        StageAction(
            key="retry",
            applied_label="retried",
            apply=lambda session, job, principal: retry_conversion_job(session, job, principal=principal.subject),
        ),
        StageAction(
            key="cancel",
            applied_label="cancelled",
            apply=lambda session, job, principal: cancel_conversion_job(session, job, principal=principal.subject),
        ),
        StageAction(
            key="delete",
            applied_label="deleted",
            apply=lambda session, job, _principal: apply_job_delete(session, job),
        ),
    ],
    detail=detail,
    detail_template="detail_conversion.html",
    extra_columns=3,
    failed_split=failed_split,
)
