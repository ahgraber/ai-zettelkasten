"""HTML UI routes for conversion jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from sqlalchemy import func
from sqlmodel import Session, select

from fastapi import APIRouter, Depends, Query, Request
from fastapi.templating import Jinja2Templates

from aizk.conversion.api.dependencies import get_db_session
from aizk.datamodel.bookmark import Bookmark
from aizk.datamodel.job import ConversionJob

router = APIRouter(prefix="/ui", tags=["ui"])

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[2] / "templates"),
)


def _format_dt(value) -> str:
    if value is None:
        return ""
    return value.isoformat()


@router.get("/jobs")
def ui_jobs(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Render the jobs Web UI."""
    total = session.exec(select(func.count()).select_from(ConversionJob)).one()
    rows = session.exec(
        select(ConversionJob, Bookmark)
        .join(Bookmark, Bookmark.aizk_uuid == ConversionJob.aizk_uuid)
        .order_by(ConversionJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    jobs = []
    for job, bookmark in rows:
        if job.id is None:
            continue
        search_text = f"{job.aizk_uuid} {bookmark.karakeep_id} {job.title}".lower()
        jobs.append(
            {
                "id": job.id,
                "aizk_uuid": str(job.aizk_uuid),
                "karakeep_id": bookmark.karakeep_id,
                "title": job.title,
                "status": job.status.value,
                "attempts": job.attempts,
                "queued_at": _format_dt(job.queued_at),
                "started_at": _format_dt(job.started_at),
                "finished_at": _format_dt(job.finished_at),
                "error_code": job.error_code or "",
                "search_text": search_text,
            }
        )

    start_index = offset + 1 if total else 0
    end_index = min(offset + limit, total)
    prev_offset = max(offset - limit, 0) if offset > 0 else None
    next_offset = offset + limit if (offset + limit) < total else None

    return _TEMPLATES.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "total": total,
            "limit": limit,
            "offset": offset,
            "start_index": start_index,
            "end_index": end_index,
            "prev_offset": prev_offset,
            "next_offset": next_offset,
        },
    )
