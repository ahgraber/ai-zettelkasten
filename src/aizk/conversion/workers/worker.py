"""Background worker for processing conversion jobs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime as dt
import json
import logging
from pathlib import Path
import tempfile
import time
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlmodel import Session, select

from aizk.conversion.datamodel.bookmark import Bookmark as BookmarkRecord
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.db import get_engine
from aizk.conversion.storage.manifest import generate_manifest, save_manifest
from aizk.conversion.storage.s3_client import S3Client, S3Error
from aizk.conversion.utilities.bookmark_utils import (
    BookmarkContentError,
    detect_content_type,
    detect_source_type,
    fetch_karakeep_bookmark,
    get_bookmark_asset_id,
    get_bookmark_html_content,
    get_bookmark_source_url,
    get_bookmark_text_content,
    is_pdf_asset,
    validate_bookmark_content,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import compute_markdown_hash
from aizk.conversion.utilities.paths import (
    OUTPUT_MARKDOWN_FILENAME,
    figure_paths,
    markdown_path,
    metadata_path,
)
from aizk.conversion.workers.converter import ConversionError, convert_html, convert_pdf
from aizk.conversion.workers.fetcher import (
    BookmarkContentUnavailableError,
    FetchError,
    fetch_arxiv,
    fetch_github_readme,
    fetch_karakeep_asset,
)
from aizk.utilities.async_utils import run_async
from aizk.utilities.url_utils import normalize_url
from karakeep_client.models import Bookmark as KarakeepBookmark

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversionInput:
    """Source bytes and processing pipeline information."""

    pipeline: Literal["html", "pdf"]
    content_bytes: bytes
    fetched_at: dt.datetime


@dataclass(frozen=True)
class ConversionArtifacts:
    """Local conversion artifacts generated in phase one."""

    markdown_path: Path
    figure_paths: list[Path]
    markdown_hash: str
    pipeline_name: str
    fetched_at: dt.datetime
    docling_version: str


class MissingArtifactsError(RuntimeError):
    """Raised when expected conversion artifacts are missing."""

    error_code = "missing_artifacts"


def _utcnow() -> dt.datetime:
    """Return timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)


def _docling_version() -> str:
    from importlib.metadata import version

    return version("docling")


def _prepare_conversion_input(
    *,
    bookmark_record: BookmarkRecord,
    karakeep_bookmark: KarakeepBookmark,
    config: ConversionConfig,
) -> ConversionInput:
    """Prepare conversion input bytes and determine pipeline."""
    fetched_at = _utcnow()
    if bookmark_record.source_type == "arxiv":
        pdf_bytes = asyncio.run(fetch_arxiv(karakeep_bookmark, config))
        return ConversionInput(pipeline="pdf", content_bytes=pdf_bytes, fetched_at=fetched_at)

    if bookmark_record.source_type == "github":
        readme_bytes = asyncio.run(fetch_github_readme(karakeep_bookmark, config))
        return ConversionInput(pipeline="html", content_bytes=readme_bytes, fetched_at=fetched_at)

    if is_pdf_asset(karakeep_bookmark):
        asset_id = get_bookmark_asset_id(karakeep_bookmark)
        if asset_id:
            pdf_bytes = run_async(fetch_karakeep_asset, asset_id)
            return ConversionInput(pipeline="pdf", content_bytes=pdf_bytes, fetched_at=fetched_at)

    # Fallback to HTML content
    html_content = get_bookmark_html_content(karakeep_bookmark)
    if html_content:
        return ConversionInput(pipeline="html", content_bytes=html_content.encode("utf-8"), fetched_at=fetched_at)

    text_content = get_bookmark_text_content(karakeep_bookmark)
    if text_content:
        html = f"<html><body><pre>{text_content}</pre></body></html>"
        return ConversionInput(pipeline="html", content_bytes=html.encode("utf-8"), fetched_at=fetched_at)

    raise BookmarkContentUnavailableError(f"Bookmark {bookmark_record.karakeep_id} has no usable content")


def _run_conversion(
    *,
    job: ConversionJob,
    bookmark: BookmarkRecord,
    conversion_input: ConversionInput,
    config: ConversionConfig,
    workspace: Path,
) -> ConversionArtifacts:
    """Run conversion phase and persist artifacts locally."""
    content_bytes = conversion_input.content_bytes
    pipeline_name = conversion_input.pipeline
    fetched_at = conversion_input.fetched_at

    if pipeline_name == "pdf":
        markdown_text, figure_paths = convert_pdf(content_bytes, workspace, config)
    else:
        markdown_text, figure_paths = convert_html(content_bytes, workspace, config, source_url=bookmark.url)

    markdown_filename = OUTPUT_MARKDOWN_FILENAME
    markdown_file = markdown_path(workspace, markdown_filename)
    markdown_file.write_text(markdown_text)
    markdown_hash = compute_markdown_hash(markdown_text)

    metadata = {
        "pipeline_name": pipeline_name,
        "fetched_at": fetched_at.isoformat(),
        "markdown_filename": markdown_filename,
        "figure_files": [path.name for path in figure_paths],
        "markdown_hash_xx64": markdown_hash,
        "docling_version": _docling_version(),
    }
    metadata_file = metadata_path(workspace)
    metadata_file.write_text(json.dumps(metadata, indent=2, sort_keys=False))

    return ConversionArtifacts(
        markdown_path=markdown_file,
        figure_paths=figure_paths,
        markdown_hash=markdown_hash,
        pipeline_name=pipeline_name,
        fetched_at=fetched_at,
        docling_version=metadata["docling_version"],
    )


def _upload_converted(job_id: int, workspace: Path) -> None:
    """Upload artifacts to S3 and record conversion output."""
    config = ConversionConfig()
    engine = get_engine(config.database_url)
    metadata_file = metadata_path(workspace)
    if not metadata_file.exists():
        raise MissingArtifactsError(f"Missing metadata for job {job_id}")

    metadata = json.loads(metadata_file.read_text())
    markdown_filename = metadata["markdown_filename"]
    markdown_file = markdown_path(workspace, markdown_filename)
    figure_files = metadata.get("figure_files", [])
    figure_file_paths = figure_paths(workspace, figure_files)

    if not markdown_file.exists():
        raise MissingArtifactsError(f"Missing markdown for job {job_id}")

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()

        s3_client = S3Client(config)
        if not s3_client.bucket:
            raise S3Error("S3 bucket is not configured", "s3_upload_failed")

        prefix = str(bookmark.aizk_uuid)
        s3_prefix_uri = f"s3://{s3_client.bucket}/{prefix}/"
        markdown_key = f"{prefix}/{markdown_filename}"
        markdown_uri = s3_client.upload_file(markdown_file, markdown_key)

        figure_uris: list[str] = []
        for fig_path in figure_file_paths:
            if not fig_path.exists():
                continue
            fig_key = f"{prefix}/figures/{fig_path.name}"
            figure_uris.append(s3_client.upload_file(fig_path, fig_key))

        job.finished_at = _utcnow()
        manifest = generate_manifest(
            bookmark=bookmark,
            job=job,
            fetched_at=dt.datetime.fromisoformat(metadata["fetched_at"]),
            markdown_s3_uri=markdown_uri,
            markdown_hash=metadata["markdown_hash_xx64"],
            figure_s3_uris=figure_uris,
            docling_version=metadata["docling_version"],
            pipeline_name=metadata["pipeline_name"],
        )
        manifest_path = workspace / "manifest.json"
        save_manifest(manifest, manifest_path)
        manifest_uri = s3_client.upload_file(manifest_path, f"{prefix}/manifest.json")

        output = ConversionOutput(
            job_id=job.id,
            aizk_uuid=bookmark.aizk_uuid,
            title=bookmark.title,
            payload_version=job.payload_version,
            s3_prefix=s3_prefix_uri,
            markdown_key=markdown_uri,
            manifest_key=manifest_uri,
            markdown_hash_xx64=metadata["markdown_hash_xx64"],
            figure_count=len(figure_uris),
            docling_version=metadata["docling_version"],
            pipeline_name=metadata["pipeline_name"],
        )
        session.add(output)

        job.status = ConversionJobStatus.SUCCEEDED
        job.error_code = None
        job.error_message = None
        job.updated_at = _utcnow()
        session.add(job)
        session.commit()


def handle_job_error(job_id: int, error: Exception) -> None:
    """Handle job error and update status with retry logic."""
    config = ConversionConfig()
    engine = get_engine(config.database_url)
    now = _utcnow()

    error_code = getattr(error, "error_code", "conversion_failed")
    message = str(error)

    permanent_errors = {
        "karakeep_bookmark_missing_contents",
        "github_readme_not_found",
        "docling_empty_output",
    }
    retryable = error_code not in permanent_errors

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        if retryable:
            delay = config.retry_base_delay_seconds * (2**job.attempts)
            job.status = ConversionJobStatus.FAILED_RETRYABLE
            job.earliest_next_attempt_at = now + dt.timedelta(seconds=delay)
        else:
            job.status = ConversionJobStatus.FAILED_PERM
            job.earliest_next_attempt_at = None
            job.finished_at = now
        job.error_code = error_code
        job.error_message = message
        job.last_error_at = now
        job.updated_at = now
        session.add(job)
        session.commit()


def recover_stale_running_jobs(config: ConversionConfig) -> int:
    """Mark stale RUNNING jobs as retryable.

    This can catch jobs that were being processed when a worker crashed.
    """
    engine = get_engine(config.database_url)
    now = _utcnow()
    stale_before = now - dt.timedelta(minutes=config.worker_stale_job_minutes)

    with Session(engine) as session:
        jobs = session.exec(
            select(ConversionJob)
            .where(ConversionJob.status == ConversionJobStatus.RUNNING)
            .where(ConversionJob.started_at.is_not(None))  # type: ignore[operator]
            .where(ConversionJob.started_at < stale_before)
        ).all()

        if not jobs:
            return 0

        for job in jobs:
            job.status = ConversionJobStatus.FAILED_RETRYABLE
            job.earliest_next_attempt_at = now
            job.error_code = "worker_stale_running"
            job.error_message = f"Marked stale after {config.worker_stale_job_minutes} minutes without completion."
            job.last_error_at = now
            job.updated_at = now
            session.add(job)

        session.commit()

    return len(jobs)


def poll_and_process_jobs() -> bool:
    """Pick up the next queued job and process it."""
    config = ConversionConfig()
    engine = get_engine(config.database_url)
    now = _utcnow()

    with Session(engine) as session:
        try:
            # BEGIN IMMEDIATE prevents multiple workers from selecting the same job.
            session.exec(text("BEGIN IMMEDIATE"))
            job = session.exec(
                select(ConversionJob)
                .where(ConversionJob.status.in_([ConversionJobStatus.QUEUED, ConversionJobStatus.FAILED_RETRYABLE]))
                .where(
                    (ConversionJob.earliest_next_attempt_at.is_(None))  # type: ignore[operator]
                    | (ConversionJob.earliest_next_attempt_at <= now)
                )
                .order_by(ConversionJob.queued_at)
            ).first()
        except OperationalError as exc:
            session.rollback()
            logger.warning("Job poll skipped due to database lock: %s", exc)
            return False
        except DBAPIError:
            session.rollback()
            logger.exception("Job poll failed due to database error")
            return False

        if not job:
            session.rollback()
            return False

        job_id = job.id
        job.status = ConversionJobStatus.RUNNING
        job.started_at = now
        job.attempts += 1
        job.updated_at = now
        session.add(job)
        session.commit()

    if job_id is None:
        raise RuntimeError("Queued job missing id; cannot process job")

    process_job(job_id)
    return True


def process_job(job_id: int) -> None:
    """Process a conversion job by ID."""
    config = ConversionConfig()
    engine = get_engine(config.database_url)
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        if job.status in {ConversionJobStatus.SUCCEEDED, ConversionJobStatus.CANCELLED}:
            return
        if job.status != ConversionJobStatus.RUNNING:
            job.status = ConversionJobStatus.RUNNING
            job.started_at = _utcnow()
            job.attempts += 1
            job.updated_at = _utcnow()
            session.add(job)
            session.commit()

        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()

    karakeep_bookmark = fetch_karakeep_bookmark(bookmark.karakeep_id)
    if not karakeep_bookmark:
        handle_job_error(job_id, FetchError(f"Bookmark {bookmark.karakeep_id} not found in KaraKeep"))
        return
    try:
        validate_bookmark_content(karakeep_bookmark)
    except BookmarkContentError as exc:
        handle_job_error(job_id, exc)
        return

    try:
        source_url = get_bookmark_source_url(karakeep_bookmark)
        updated_source_type = detect_source_type(source_url)
        updated_content_type = detect_content_type(karakeep_bookmark)
        updated_title = karakeep_bookmark.title or source_url
        normalized_url = normalize_url(source_url) if source_url else None
    except BookmarkContentError as exc:
        handle_job_error(job_id, exc)
        return

    with Session(engine) as session:
        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()
        job_record = session.get(ConversionJob, job_id)
        bookmark.url = source_url
        bookmark.normalized_url = normalized_url
        bookmark.title = updated_title
        bookmark.content_type = updated_content_type
        bookmark.source_type = updated_source_type
        bookmark.updated_at = _utcnow()
        if job_record:
            job_record.title = updated_title
            job_record.updated_at = _utcnow()
            session.add(job_record)
        session.add(bookmark)
        session.commit()
        session.refresh(bookmark)
        if job_record:
            session.refresh(job_record)
            job = job_record

    with tempfile.TemporaryDirectory() as tmpdirname:
        workspace = Path(tmpdirname)
        try:
            conversion_input = _prepare_conversion_input(
                bookmark_record=bookmark,
                karakeep_bookmark=karakeep_bookmark,
                config=config,
            )
            _run_conversion(
                job=job,
                bookmark=bookmark,
                config=config,
                workspace=workspace,
                conversion_input=conversion_input,
            )
            with Session(engine) as session:
                job = session.get(ConversionJob, job_id)
                if job:
                    job.status = ConversionJobStatus.UPLOAD_PENDING
                    job.updated_at = _utcnow()
                    session.add(job)
                    session.commit()
        except (ConversionError, FetchError, BookmarkContentUnavailableError, BookmarkContentError) as exc:
            handle_job_error(job_id, exc)
            return
        except Exception as exc:
            handle_job_error(job_id, exc)
            return

        for attempt in range(1, config.retry_max_attempts + 1):
            try:
                _upload_converted(job_id, workspace)
                break
            except Exception as exc:
                if attempt == config.retry_max_attempts:
                    handle_job_error(job_id, exc)
                    break
                delay = config.retry_base_delay_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "Upload attempt %d failed for job %s; retrying in %s seconds: %s",
                    attempt,
                    job_id,
                    delay,
                    exc,
                )
                time.sleep(delay)


def run_worker(poll_interval_seconds: float = 2.0) -> None:
    """Run the worker loop for processing jobs."""
    logger.info("Starting conversion worker loop")
    config = ConversionConfig()
    last_recovery_check = 0.0
    while True:
        now = time.monotonic()
        if now - last_recovery_check >= config.worker_stale_job_check_seconds:
            recovered = recover_stale_running_jobs(config)
            if recovered:
                logger.warning("Recovered %d stale RUNNING jobs", recovered)
            last_recovery_check = now
        processed = poll_and_process_jobs()
        if not processed:
            time.sleep(poll_interval_seconds)
