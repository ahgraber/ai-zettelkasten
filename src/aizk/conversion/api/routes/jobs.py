"""API routes for conversion jobs."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated
from uuid import UUID

from pydantic import AnyUrl
from sqlalchemy import func, text
from sqlalchemy.orm import joinedload, selectinload
from sqlmodel import Session, select

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from aizk.conversion.api.dependencies import get_config, get_db_session
from aizk.conversion.api.schemas import (
    ArtifactSummary,
    BulkActionResponse,
    BulkActionResult,
    BulkActionSummary,
    BulkJobActionRequest,
    JobList,
    JobResponse,
    JobStatusCounts,
    JobSubmission,
    QueueFullResponse,
)
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source as Bookmark
from aizk.conversion.utilities.hashing import compute_idempotency_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


def _utcnow() -> dt.datetime:
    """Return timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)


def _job_to_response(
    job: ConversionJob,
    bookmark: Bookmark,
    output: ConversionOutput | None,
) -> JobResponse:
    """Convert job and optional output into API response."""
    if job.id is None:
        raise ValueError("ConversionJob.id must be set before response generation")

    artifact_summary = None
    if output:
        artifact_summary = ArtifactSummary(
            s3_prefix=output.s3_prefix,
            markdown_key=output.markdown_key,
            manifest_key=output.manifest_key,
            figure_count=output.figure_count,
        )

    bookmark_url = AnyUrl(bookmark.url) if bookmark.url else None
    bookmark_title = bookmark.title or job.title

    return JobResponse(
        id=job.id,
        aizk_uuid=job.aizk_uuid,
        karakeep_id=bookmark.karakeep_id,
        url=bookmark_url,
        title=bookmark_title,
        source_type=bookmark.source_type,
        status=job.status,
        attempts=job.attempts,
        payload_version=job.payload_version,
        idempotency_key=job.idempotency_key,
        error_code=job.error_code,
        error_message=job.error_message,
        earliest_next_attempt_at=job.earliest_next_attempt_at,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        artifact_summary=artifact_summary,
    )


def _get_output_summary(session: Session, job_id: int) -> ConversionOutput | None:
    """Load conversion output for a job."""
    return session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).first()


def _apply_job_retry(job: ConversionJob, now: dt.datetime) -> None:
    """Apply retry transition to a conversion job."""
    if job.status not in {
        ConversionJobStatus.FAILED_RETRYABLE,
        ConversionJobStatus.FAILED_PERM,
        ConversionJobStatus.CANCELLED,
    }:
        raise ValueError("job_not_retryable")
    job.attempts += 1
    job.status = ConversionJobStatus.QUEUED
    job.earliest_next_attempt_at = None
    job.last_error_at = None
    job.error_code = None
    job.error_message = None
    job.queued_at = now
    job.started_at = None
    job.finished_at = None
    job.updated_at = now


def _apply_job_cancel(job: ConversionJob, now: dt.datetime) -> None:
    """Apply cancel transition to a conversion job."""
    if job.status not in {
        ConversionJobStatus.QUEUED,
        ConversionJobStatus.RUNNING,
        ConversionJobStatus.FAILED_RETRYABLE,
    }:
        raise ValueError("job_not_cancellable")
    job.status = ConversionJobStatus.CANCELLED
    job.finished_at = now
    job.earliest_next_attempt_at = None
    job.updated_at = now


def _apply_job_delete(session: Session, job: ConversionJob) -> None:
    """Apply delete transition to a conversion job."""
    if job.status not in {
        ConversionJobStatus.FAILED_RETRYABLE,
        ConversionJobStatus.FAILED_PERM,
        ConversionJobStatus.CANCELLED,
    }:
        raise ValueError("job_not_deletable")

    output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job.id)).first()
    if output:
        session.delete(output)
    session.delete(job)


@router.post(
    "",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    responses={503: {"model": QueueFullResponse, "description": "Queue is at capacity"}},
)
def submit_job(
    submission: JobSubmission,
    api_response: Response,
    session: Annotated[Session, Depends(get_db_session)],
    request: Request,
) -> JobResponse:
    """Submit a new conversion job."""
    config = get_config(request)

    bookmark = session.exec(select(Bookmark).where(Bookmark.karakeep_id == submission.karakeep_id)).first()
    is_new_bookmark = bookmark is None
    if is_new_bookmark:
        # Create in memory only.  aizk_uuid is set by the Python default
        # factory (uuid4), so it is available before the row is persisted.
        # The INSERT is deferred to after the queue-depth check so a
        # rejected submission does not leave an orphan bookmark row.
        bookmark = Bookmark(
            karakeep_id=submission.karakeep_id,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )

    # End the auto-begun read transaction so BEGIN IMMEDIATE can start clean.
    session.commit()

    # BEGIN IMMEDIATE acquires the write lock upfront so the subsequent
    # read-then-write sequence (idempotency check → queue depth → INSERT)
    # cannot hit a non-retriable SQLITE_BUSY_SNAPSHOT on lock upgrade.
    # Mirrors the worker's claim_next_job pattern in loop.py.
    session.exec(text("BEGIN IMMEDIATE"))

    idempotency_key = submission.idempotency_key or compute_idempotency_key(
        bookmark.aizk_uuid,
        submission.payload_version,
        config,
        picture_description_enabled=config.is_picture_description_enabled(),
    )

    existing_job = session.exec(select(ConversionJob).where(ConversionJob.idempotency_key == idempotency_key)).first()
    if existing_job:
        output = _get_output_summary(session, existing_job.id)
        bookmark = session.exec(select(Bookmark).where(Bookmark.aizk_uuid == existing_job.aizk_uuid)).one()
        api_response.status_code = status.HTTP_200_OK
        job_response = _job_to_response(existing_job, bookmark, output)
        return job_response

    actionable_statuses = [ConversionJobStatus.QUEUED, ConversionJobStatus.FAILED_RETRYABLE]
    queue_depth = session.exec(
        select(func.count()).select_from(ConversionJob).where(ConversionJob.status.in_(actionable_statuses))
    ).one()
    if queue_depth >= config.queue_max_depth:
        session.rollback()
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Queue is at capacity", "retry_after": config.queue_retry_after_seconds},
            headers={"Retry-After": str(config.queue_retry_after_seconds)},
        )

    if is_new_bookmark:
        session.add(bookmark)
        session.flush()

    now = _utcnow()
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title or bookmark.karakeep_id,
        payload_version=submission.payload_version,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key=idempotency_key,
        queued_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    session.refresh(bookmark)

    job_response = _job_to_response(job, bookmark, None)
    return job_response


@router.get("/status-counts", response_model=JobStatusCounts)
def get_job_status_counts(
    session: Annotated[Session, Depends(get_db_session)],
) -> JobStatusCounts:
    """Return aggregated counts of jobs by status."""
    rows = session.exec(select(ConversionJob.status, func.count()).group_by(ConversionJob.status)).all()
    counts: dict[str, int] = {}
    for status_, count in rows:
        key = status_.value if isinstance(status_, ConversionJobStatus) else str(status_)
        counts[key] = count
    total = sum(counts.values())
    return JobStatusCounts(counts=counts, total=total)


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: int,
    session: Annotated[Session, Depends(get_db_session)],
) -> JobResponse:
    """Get conversion job details."""
    job = session.get(ConversionJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "job_not_found", "message": "Job not found"})
    output = _get_output_summary(session, job_id)
    bookmark = session.exec(select(Bookmark).where(Bookmark.aizk_uuid == job.aizk_uuid)).one()
    return _job_to_response(job, bookmark, output)


@router.get("", response_model=JobList)
def list_jobs(
    session: Annotated[Session, Depends(get_db_session)],
    status_filter: Annotated[ConversionJobStatus | None, Query(alias="status")] = None,
    aizk_uuid: Annotated[UUID | None, Query()] = None,
    karakeep_id: Annotated[str | None, Query()] = None,
    created_after: Annotated[dt.datetime | None, Query()] = None,
    created_before: Annotated[dt.datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobList:
    """List conversion jobs with filters."""
    query = select(ConversionJob)
    count_query = select(ConversionJob)

    if status_filter:
        query = query.where(ConversionJob.status == status_filter)
        count_query = count_query.where(ConversionJob.status == status_filter)
    if aizk_uuid:
        query = query.where(ConversionJob.aizk_uuid == aizk_uuid)
        count_query = count_query.where(ConversionJob.aizk_uuid == aizk_uuid)
    if karakeep_id:
        query = query.join(Bookmark).where(Bookmark.karakeep_id == karakeep_id)
        count_query = count_query.join(Bookmark).where(Bookmark.karakeep_id == karakeep_id)
    if created_after:
        query = query.where(ConversionJob.created_at >= created_after)
        count_query = count_query.where(ConversionJob.created_at >= created_after)
    if created_before:
        query = query.where(ConversionJob.created_at <= created_before)
        count_query = count_query.where(ConversionJob.created_at <= created_before)

    count_stmt = select(func.count()).select_from(count_query.subquery())
    total = session.exec(count_stmt).one()
    # N+1 happens when each job triggers its own query for related rows
    # (1 query for jobs + N queries for relations).
    # joinedload/selectinload eager-load in bulk to keep the number of queries bounded.
    jobs = session.exec(
        query.options(
            joinedload(ConversionJob.source),
            selectinload(ConversionJob.output),
        )
        .order_by(ConversionJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    responses = []
    for job in jobs:
        if job.source is None:
            raise HTTPException(status_code=500, detail={"error": "source_missing", "message": "Source not found"})
        responses.append(_job_to_response(job, job.source, job.output))

    return JobList(jobs=responses, total=total, limit=limit, offset=offset)


@router.post("/{job_id}/retry", response_model=JobResponse)
def retry_job(
    job_id: int,
    session: Annotated[Session, Depends(get_db_session)],
) -> JobResponse:
    """Retry a failed or cancelled job."""
    session.exec(text("BEGIN IMMEDIATE"))
    job = session.get(ConversionJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "job_not_found", "message": "Job not found"})
    now = _utcnow()
    try:
        _apply_job_retry(job, now)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "message": "Job cannot be retried"},
        ) from exc
    session.add(job)
    session.commit()
    session.refresh(job)
    bookmark = session.exec(select(Bookmark).where(Bookmark.aizk_uuid == job.aizk_uuid)).one()
    output = _get_output_summary(session, job_id)
    return _job_to_response(job, bookmark, output)


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: int,
    session: Annotated[Session, Depends(get_db_session)],
) -> JobResponse:
    """Cancel a queued or running job."""
    session.exec(text("BEGIN IMMEDIATE"))
    job = session.get(ConversionJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"error": "job_not_found", "message": "Job not found"})
    now = _utcnow()
    try:
        _apply_job_cancel(job, now)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "message": "Job cannot be cancelled"},
        ) from exc
    session.add(job)
    session.commit()
    session.refresh(job)
    bookmark = session.exec(select(Bookmark).where(Bookmark.aizk_uuid == job.aizk_uuid)).one()
    output = _get_output_summary(session, job_id)
    return _job_to_response(job, bookmark, output)


@router.post("/actions", response_model=BulkActionResponse)
def bulk_job_actions(
    payload: BulkJobActionRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> BulkActionResponse:
    """Apply retry or cancel actions across multiple jobs."""
    session.exec(text("BEGIN IMMEDIATE"))
    now = _utcnow()
    results: list[BulkActionResult] = []
    success = 0
    errors = 0

    for job_id in payload.job_ids:
        job = session.get(ConversionJob, job_id)
        if not job:
            results.append(BulkActionResult(job_id=job_id, status="error", error="job_not_found"))
            errors += 1
            continue
        try:
            if payload.action == "retry":
                _apply_job_retry(job, now)
            else:
                _apply_job_cancel(job, now)
            session.add(job)
            results.append(BulkActionResult(job_id=job_id, status="success", error=None))
            success += 1
        except ValueError as exc:
            results.append(BulkActionResult(job_id=job_id, status="error", error=str(exc)))
            errors += 1

    session.commit()
    summary = BulkActionSummary(success=success, errors=errors)
    return BulkActionResponse(action=payload.action, results=results, summary=summary)
