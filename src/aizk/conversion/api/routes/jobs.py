"""API routes for conversion jobs."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated
from uuid import uuid4

from pydantic import AnyUrl
from sqlmodel import Session, select

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from aizk.conversion.api.dependencies import get_db_session
from aizk.conversion.api.schemas import ArtifactSummary, JobList, JobResponse, JobSubmission
from aizk.conversion.utilities.bookmark_utils import (
    detect_content_type,
    detect_source_type,
    fetch_karakeep_bookmark,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import compute_idempotency_key
from aizk.datamodel.bookmark import Bookmark
from aizk.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.datamodel.output import ConversionOutput
from aizk.utilities.url_utils import normalize_url

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

    return JobResponse(
        id=job.id,
        aizk_uuid=job.aizk_uuid,
        karakeep_id=bookmark.karakeep_id,
        url=AnyUrl(bookmark.url),
        title=bookmark.title,
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


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def submit_job(
    submission: JobSubmission,
    api_response: Response,
    session: Annotated[Session, Depends(get_db_session)],
) -> JobResponse:
    """Submit a new conversion job."""
    config = ConversionConfig()
    source_url = submission.url
    source_type = submission.source_type or detect_source_type(str(source_url))

    bookmark = session.exec(select(Bookmark).where(Bookmark.karakeep_id == submission.karakeep_id)).first()
    if not bookmark:
        karakeep_bookmark = fetch_karakeep_bookmark(submission.karakeep_id)
        if karakeep_bookmark:
            content_type = detect_content_type(karakeep_bookmark)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "karakeep_bookmark_not_found",
                    "message": f"KaraKeep bookmark not found for {submission.karakeep_id}",
                },
            )
        aizk_uuid = submission.aizk_uuid or str(uuid4())
        bookmark = Bookmark(
            karakeep_id=submission.karakeep_id,
            aizk_uuid=aizk_uuid,
            url=str(source_url),
            normalized_url=normalize_url(str(source_url)),
            title=submission.title,
            content_type=content_type,
            source_type=source_type,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        session.add(bookmark)
        session.commit()
        session.refresh(bookmark)

    idempotency_key = submission.idempotency_key or compute_idempotency_key(
        bookmark.aizk_uuid,
        submission.payload_version,
        config,
    )

    existing_job = session.exec(select(ConversionJob).where(ConversionJob.idempotency_key == idempotency_key)).first()
    if existing_job:
        output = _get_output_summary(session, existing_job.id)
        bookmark = session.exec(select(Bookmark).where(Bookmark.aizk_uuid == existing_job.aizk_uuid)).one()
        api_response.status_code = status.HTTP_200_OK
        job_response = _job_to_response(existing_job, bookmark, output)
        return job_response

    now = _utcnow()
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
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

    job_response = _job_to_response(job, bookmark, None)
    return job_response


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
    aizk_uuid: Annotated[str | None, Query()] = None,
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

    total = len(session.exec(count_query).all())
    jobs = session.exec(query.order_by(ConversionJob.created_at.desc()).limit(limit).offset(offset)).all()

    responses = []
    for job in jobs:
        output = _get_output_summary(session, job.id)
        bookmark = session.exec(select(Bookmark).where(Bookmark.aizk_uuid == job.aizk_uuid)).one()
        responses.append(_job_to_response(job, bookmark, output))

    return JobList(jobs=responses, total=total, limit=limit, offset=offset)
