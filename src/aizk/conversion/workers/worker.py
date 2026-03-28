"""Background worker for processing conversion jobs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime as dt
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import queue as queue_module
import tempfile
import time
from typing import ClassVar, Literal

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlmodel import Session, select

from aizk.conversion.datamodel.bookmark import Bookmark as BookmarkRecord
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.db import get_engine
from aizk.conversion.storage.manifest import (
    ManifestConfigSnapshot,
    generate_manifest,
    save_manifest,
)
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
    is_precrawled_archive_asset,
    validate_bookmark_content,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import build_output_config_snapshot, compute_markdown_hash
from aizk.conversion.utilities.paths import (
    OUTPUT_MARKDOWN_FILENAME,
    figure_paths,
    markdown_path,
    metadata_path,
)
from aizk.conversion.utilities.whitespace import normalize_whitespace
from aizk.conversion.workers.converter import (
    ConversionError,
    convert_html,
    convert_pdf,
)
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


class ConversionArtifactsMissingError(RuntimeError):
    """Raised when expected conversion artifacts are missing."""

    error_code = "conversion_artifacts_missing"
    retryable: ClassVar[bool] = False


class ConversionCancelledError(RuntimeError):
    """Raised when a conversion job is cancelled during processing."""

    error_code = "conversion_cancelled"
    retryable: ClassVar[bool] = False


class ConversionTimeoutError(RuntimeError):
    """Raised when a conversion job exceeds the configured timeout."""

    error_code = "conversion_timeout"
    retryable: ClassVar[bool] = True

    def __init__(self, message: str, phase: str) -> None:
        super().__init__(message)
        self.phase = phase


class ConversionSubprocessError(RuntimeError):
    """Raised when the conversion subprocess exits unexpectedly."""

    error_code = "conversion_subprocess_failed"
    retryable: ClassVar[bool] = True


class JobDataIntegrityError(RuntimeError):
    """Raised when job data invariants are violated."""

    error_code = "job_data_integrity"
    retryable: ClassVar[bool] = False


class ReportedChildError(RuntimeError):
    """Raised when a child process reports a failure."""

    retryable: ClassVar[bool] = True

    def __init__(self, message: str, error_code: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        if retryable is not None:
            self.retryable = retryable


class PreflightError(RuntimeError):
    """Raised when preflight validation fails unexpectedly."""

    error_code = "conversion_preflight_failed"
    retryable: ClassVar[bool] = True


def _utcnow() -> dt.datetime:
    """Return timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)


@dataclass(frozen=True, slots=True)
class SupervisionResult:
    """Return values for conversion subprocess supervision."""

    last_phase: str
    reported_error: dict[str, str] | None
    cancelled: bool
    timed_out: bool


def _docling_version() -> str:
    """Return the installed docling version for conversion metadata."""
    from importlib.metadata import version

    return version("docling")


def _raise_if_cancelled(job_id: int, engine: Engine) -> None:
    """Raise if the job status has been marked as cancelled."""
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if job and job.status == ConversionJobStatus.CANCELLED:
            raise ConversionCancelledError(f"Job {job_id} cancelled")


def _is_job_cancelled(job_id: int, engine: Engine) -> bool:
    """Return True when the job status is CANCELLED."""
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        return bool(job and job.status == ConversionJobStatus.CANCELLED)


def _report_status(
    status_queue: mp.Queue | None,
    *,
    event: Literal["phase", "completed", "cancelled", "failed"],
    message: str,
    error_code: str | None = None,
    retryable: bool | None = None,
) -> None:
    """Send a structured event from the subprocess to the parent."""
    if not status_queue:
        return
    payload: dict[str, str] = {"event": event, "message": message}
    if error_code:
        payload["error_code"] = error_code
    if retryable is not None:
        payload["retryable"] = "true" if retryable else "false"
    try:
        status_queue.put_nowait(payload)
    except Exception:
        logger.debug(
            "Failed to report status event %s with message %s",
            event,
            message,
            exc_info=True,
        )
        return


def _prepare_bookmark_for_job(job_id: int, engine: Engine) -> tuple[BookmarkRecord, KarakeepBookmark]:
    """Fetch, validate, and persist AIZK Bookmark for conversion.

    Runs in the parent process before spawning the conversion subprocess.
    """
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            raise JobDataIntegrityError(f"Job {job_id} missing during preflight")
        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()

    karakeep_bookmark = fetch_karakeep_bookmark(bookmark.karakeep_id)
    if not karakeep_bookmark:
        raise FetchError(f"Bookmark {bookmark.karakeep_id} not found in KaraKeep")
    validate_bookmark_content(karakeep_bookmark)

    source_url = get_bookmark_source_url(karakeep_bookmark)
    updated_source_type = detect_source_type(source_url)
    updated_content_type = detect_content_type(karakeep_bookmark)
    updated_title = karakeep_bookmark.title or source_url
    normalized_url = normalize_url(source_url) if source_url else None

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

    return bookmark, karakeep_bookmark


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

    if is_precrawled_archive_asset(karakeep_bookmark):
        asset_id = get_bookmark_asset_id(karakeep_bookmark)
        if asset_id:
            html_bytes = run_async(fetch_karakeep_asset, asset_id)
            return ConversionInput(pipeline="html", content_bytes=html_bytes, fetched_at=fetched_at)

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
    engine: Engine,
    workspace: Path,
) -> ConversionArtifacts:
    """Run conversion and persist local artifacts for parent upload.

    This is executed in the child process and performs cancellation checks.
    """
    _raise_if_cancelled(job.id, engine)
    content_bytes = conversion_input.content_bytes
    pipeline_name = conversion_input.pipeline
    fetched_at = conversion_input.fetched_at

    if pipeline_name == "pdf":
        markdown_text, figure_paths = convert_pdf(content_bytes, workspace, config)
    else:
        markdown_text, figure_paths = convert_html(content_bytes, workspace, config, source_url=bookmark.url)

    _raise_if_cancelled(job.id, engine)
    markdown_filename = OUTPUT_MARKDOWN_FILENAME
    markdown_file = markdown_path(workspace, markdown_filename)
    # Normalize whitespace before writing and hashing
    markdown_text = normalize_whitespace(markdown_text)
    markdown_file.write_text(markdown_text)
    markdown_hash = compute_markdown_hash(markdown_text)
    picture_description_enabled = config.is_picture_description_enabled()
    config_snapshot = build_output_config_snapshot(
        config,
        picture_description_enabled=picture_description_enabled,
    )

    metadata = {
        "pipeline_name": pipeline_name,
        "fetched_at": fetched_at.isoformat(),
        "markdown_filename": markdown_filename,
        "figure_files": [path.name for path in figure_paths],
        "markdown_hash_xx64": markdown_hash,
        "docling_version": _docling_version(),
        "config_snapshot": config_snapshot,
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


def _convert_job_artifacts(
    *,
    job_id: int,
    workspace: Path,
    karakeep_payload_path: Path,
    status_queue: mp.Queue | None,
) -> None:
    """Prepare input and run conversion in the child process."""
    config = ConversionConfig()
    engine = get_engine(config.database_url)

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            raise JobDataIntegrityError(f"Job {job_id} missing during conversion")
        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()

    _raise_if_cancelled(job_id, engine)
    karakeep_payload = json.loads(karakeep_payload_path.read_text())
    karakeep_bookmark = KarakeepBookmark.model_validate(karakeep_payload)

    _report_status(status_queue, event="phase", message="preparing_input")
    conversion_input = _prepare_conversion_input(
        bookmark_record=bookmark,
        karakeep_bookmark=karakeep_bookmark,
        config=config,
    )

    _report_status(status_queue, event="phase", message="converting")
    _run_conversion(
        job=job,
        bookmark=bookmark,
        config=config,
        workspace=workspace,
        conversion_input=conversion_input,
        engine=engine,
    )


def _upload_converted(job_id: int, workspace: Path, config: ConversionConfig) -> None:
    """Upload artifacts to S3 and record conversion output in the DB."""
    engine = get_engine(config.database_url)
    metadata_file = metadata_path(workspace)
    if not metadata_file.exists():
        raise ConversionArtifactsMissingError(f"Missing metadata for job {job_id}")

    metadata = json.loads(metadata_file.read_text())
    markdown_filename = metadata["markdown_filename"]
    markdown_file = markdown_path(workspace, markdown_filename)
    figure_files = metadata.get("figure_files", [])
    figure_file_paths = figure_paths(workspace, figure_files)

    if not markdown_file.exists():
        raise ConversionArtifactsMissingError(f"Missing markdown for job {job_id}")

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        if job.status == ConversionJobStatus.CANCELLED:
            return
        bookmark = session.exec(select(BookmarkRecord).where(BookmarkRecord.aizk_uuid == job.aizk_uuid)).one()

        # Reuse existing S3 artifacts when the content hash matches a prior output for
        # the same bookmark, avoiding redundant uploads of identical content.
        new_hash = metadata["markdown_hash_xx64"]
        prior_output = session.exec(
            select(ConversionOutput)
            .where(ConversionOutput.aizk_uuid == bookmark.aizk_uuid)
            .where(ConversionOutput.markdown_hash_xx64 == new_hash)
            .order_by(ConversionOutput.created_at.desc())
        ).first()

        if prior_output is not None:
            logger.info(
                "Job %s: content hash matches prior output %s; reusing S3 artifacts at %s",
                job_id,
                prior_output.id,
                prior_output.s3_prefix,
            )
            output = ConversionOutput(
                job_id=job.id,
                aizk_uuid=bookmark.aizk_uuid,
                title=bookmark.title,
                payload_version=job.payload_version,
                s3_prefix=prior_output.s3_prefix,
                markdown_key=prior_output.markdown_key,
                manifest_key=prior_output.manifest_key,
                markdown_hash_xx64=new_hash,
                figure_count=prior_output.figure_count,
                docling_version=metadata["docling_version"],
                pipeline_name=metadata["pipeline_name"],
            )
            session.add(output)
            job.finished_at = _utcnow()
            job.status = ConversionJobStatus.SUCCEEDED
            job.error_code = None
            job.error_message = None
            job.updated_at = _utcnow()
            session.add(job)
            session.commit()
            return

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
            config_snapshot=ManifestConfigSnapshot(**metadata["config_snapshot"]),
        )
        manifest_path = workspace / "manifest.json"
        save_manifest(manifest, manifest_path)
        manifest_uri = s3_client.upload_file(manifest_path, f"{prefix}/manifest.json")

        session.refresh(job)
        if job.status == ConversionJobStatus.CANCELLED:
            return

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


def _process_job_subprocess(
    job_id: int,
    workspace_path: str,
    karakeep_payload_path: str,
    status_queue: mp.Queue,
) -> None:
    """Subprocess entrypoint that reports conversion events to the parent."""
    os.setpgrp()  # Create new process group for cleanup of all descendants
    try:
        _convert_job_artifacts(
            job_id=job_id,
            workspace=Path(workspace_path),
            karakeep_payload_path=Path(karakeep_payload_path),
            status_queue=status_queue,
        )
        _report_status(status_queue, event="completed", message="conversion completed")
    except ConversionCancelledError:
        _report_status(status_queue, event="cancelled", message="conversion cancelled")
    except (
        ConversionError,
        FetchError,
        BookmarkContentUnavailableError,
        BookmarkContentError,
        JobDataIntegrityError,
    ) as exc:
        error_code = getattr(exc, "error_code", "conversion_failed")
        retryable = exc.retryable
        _report_status(
            status_queue,
            event="failed",
            message=str(exc),
            error_code=error_code,
            retryable=retryable,
        )
        raise
    except Exception as exc:
        _report_status(
            status_queue,
            event="failed",
            message=str(exc),
            error_code="conversion_failed",
            retryable=True,
        )
        raise


def _initialize_running_job(job_id: int, engine: Engine) -> bool:
    """Ensure the job is in RUNNING state before processing."""
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return False
        if job.status in {ConversionJobStatus.SUCCEEDED, ConversionJobStatus.CANCELLED}:
            return False
        if job.status != ConversionJobStatus.RUNNING:
            job.status = ConversionJobStatus.RUNNING
            job.started_at = _utcnow()
            job.attempts += 1
            job.updated_at = _utcnow()
            session.add(job)
            session.commit()
    return True


def _spawn_conversion_subprocess(
    *,
    job_id: int,
    workspace: Path,
    payload_path: Path,
) -> tuple[mp.Process, mp.Queue]:
    """Start the conversion subprocess and return the process and status queue."""
    ctx = mp.get_context("spawn")
    status_queue: mp.Queue = ctx.Queue()
    process = ctx.Process(
        target=_process_job_subprocess,
        args=(job_id, str(workspace), str(payload_path), status_queue),
        daemon=True,
    )
    process.start()
    return process, status_queue


def _get_parent_pgid() -> int | None:
    """Return the parent process group id, if available."""
    try:
        return os.getpgrp()
    except OSError:
        return None


def _terminate_child_process(process: mp.Process, parent_pgid: int | None, sig: int) -> None:
    """Terminate the child process or its process group safely."""
    if not process.pid:
        return
    try:
        pgid = os.getpgid(process.pid)
        if parent_pgid is not None and pgid == parent_pgid:
            os.kill(process.pid, sig)
        else:
            os.killpg(pgid, sig)
    except (ProcessLookupError, OSError):
        return


def _collect_status_messages(
    *,
    job_id: int,
    status_queue: mp.Queue,
    last_phase: str,
    reported_error: dict[str, str] | None,
) -> tuple[str, dict[str, str] | None]:
    """Drain the status queue, updating phase and error state."""
    try:
        while True:
            message = status_queue.get_nowait()
            event = message.get("event")
            if event == "phase":
                new_phase = message.get("message", last_phase)
                if new_phase != last_phase:
                    last_phase = new_phase
                    logger.info("Job %s entered phase %s", job_id, last_phase)
            elif event == "failed":
                reported_error = message
    except queue_module.Empty:
        pass
    return last_phase, reported_error


def _supervise_conversion_process(
    *,
    job_id: int,
    engine: Engine,
    process: mp.Process,
    status_queue: mp.Queue,
    poll_interval_seconds: float,
    deadline: float | None,
    timeout_seconds: float,
    config: ConversionConfig,
) -> SupervisionResult:
    """Monitor the subprocess for cancellation or timeout."""
    import signal

    last_phase = "starting"
    reported_error: dict[str, str] | None = None
    parent_pgid = _get_parent_pgid()

    while process.is_alive():
        last_phase, reported_error = _collect_status_messages(
            job_id=job_id,
            status_queue=status_queue,
            last_phase=last_phase,
            reported_error=reported_error,
        )

        if _is_job_cancelled(job_id, engine):
            _terminate_child_process(process, parent_pgid, signal.SIGTERM)
            process.join(timeout=5.0)
            if process.is_alive() and process.pid:
                _terminate_child_process(process, parent_pgid, signal.SIGKILL)
                process.join(timeout=5.0)
            logger.info("Job %s cancelled during %s", job_id, last_phase)
            return SupervisionResult(last_phase, reported_error, True, False)

        if deadline and time.monotonic() >= deadline:
            _terminate_child_process(process, parent_pgid, signal.SIGTERM)
            process.join(timeout=5.0)
            if process.is_alive() and process.pid:
                _terminate_child_process(process, parent_pgid, signal.SIGKILL)
                process.join(timeout=5.0)
            elapsed = None
            if deadline is not None:
                elapsed = time.monotonic() - (deadline - timeout_seconds)
            logger.info(
                "Job %s timed out during %s after %s seconds",
                job_id,
                last_phase,
                round(elapsed, 3) if elapsed is not None else "unknown",
            )
            handle_job_error(
                job_id,
                ConversionTimeoutError(
                    f"Job {job_id} exceeded its runtime during {last_phase}",
                    last_phase,
                ),
                config,
            )
            return SupervisionResult(last_phase, reported_error, False, True)

        process.join(timeout=poll_interval_seconds)

    last_phase, reported_error = _collect_status_messages(
        job_id=job_id,
        status_queue=status_queue,
        last_phase=last_phase,
        reported_error=reported_error,
    )
    return SupervisionResult(last_phase, reported_error, False, False)


def process_job_supervised(job_id: int, config: ConversionConfig, poll_interval_seconds: float = 2.0) -> None:
    """Run a supervised conversion attempt and upload artifacts on success.

    The parent process handles preflight, cancellation, timeout, and uploads.
    """
    engine = get_engine(config.database_url)
    timeout_seconds = float(config.worker_job_timeout_seconds)

    if not _initialize_running_job(job_id, engine):
        return

    try:
        bookmark, karakeep_bookmark = _prepare_bookmark_for_job(job_id, engine)
    except (FetchError, BookmarkContentError, JobDataIntegrityError) as exc:
        handle_job_error(job_id, exc, config)
        return
    except Exception as exc:
        handle_job_error(job_id, PreflightError(f"Job {job_id} preflight failed: {exc}"), config)
        return
    with tempfile.TemporaryDirectory() as tmpdirname:
        workspace = Path(tmpdirname)
        payload_path = workspace / "karakeep_bookmark.json"
        payload_path.write_text(json.dumps(karakeep_bookmark.model_dump(mode="json"), indent=2))

        process, status_queue = _spawn_conversion_subprocess(
            job_id=job_id,
            workspace=workspace,
            payload_path=payload_path,
        )

        deadline = None
        if timeout_seconds > 0:
            deadline = time.monotonic() + timeout_seconds

        result = _supervise_conversion_process(
            job_id=job_id,
            engine=engine,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=poll_interval_seconds,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            config=config,
        )

        if result.cancelled or result.timed_out:
            return

        if result.reported_error:
            error_code = result.reported_error.get("error_code", "conversion_failed")
            error_message = result.reported_error.get("message", "conversion_failed")
            retryable = None
            retryable_value = result.reported_error.get("retryable")
            if retryable_value is not None:
                retryable = str(retryable_value).lower() == "true"
            handle_job_error(
                job_id,
                ReportedChildError(
                    error_message,
                    error_code,
                    retryable=retryable,
                ),
                config,
            )
            return

        if process.exitcode and process.exitcode != 0:
            handle_job_error(
                job_id,
                ConversionSubprocessError(f"Job {job_id} subprocess exited with code {process.exitcode}"),
                config,
            )
            return

        if _is_job_cancelled(job_id, engine):
            logger.info("Job %s cancelled before upload", job_id)
            return

        last_phase = "uploading"
        if deadline and time.monotonic() >= deadline:
            elapsed = time.monotonic() - (deadline - timeout_seconds)
            logger.info(
                "Job %s timed out during %s after %s seconds",
                job_id,
                last_phase,
                round(elapsed, 3),
            )
            handle_job_error(
                job_id,
                ConversionTimeoutError(
                    f"Job {job_id} exceeded its runtime during {last_phase}",
                    last_phase,
                ),
                config,
            )
            return

        with Session(engine) as session:
            job_record = session.get(ConversionJob, job_id)
            if job_record:
                job_record.status = ConversionJobStatus.UPLOAD_PENDING
                job_record.updated_at = _utcnow()
                session.add(job_record)
                session.commit()

        for attempt in range(1, config.retry_max_attempts + 1):
            if deadline and time.monotonic() >= deadline:
                elapsed = time.monotonic() - (deadline - timeout_seconds)
                logger.info(
                    "Job %s timed out during %s after %s seconds",
                    job_id,
                    last_phase,
                    round(elapsed, 3),
                )
                handle_job_error(
                    job_id,
                    ConversionTimeoutError(
                        f"Job {job_id} exceeded its runtime during {last_phase}",
                        last_phase,
                    ),
                    config,
                )
                return
            try:
                _upload_converted(job_id, workspace, config)
                break
            except Exception as exc:
                if attempt == config.retry_max_attempts:
                    handle_job_error(job_id, exc, config)
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


def handle_job_error(job_id: int, error: Exception, config: ConversionConfig) -> None:
    """Persist job failure details and compute retryability.

    Retry decision uses the `retryable` class attribute on every exception class.
    """
    engine = get_engine(config.database_url)
    now = _utcnow()

    error_code = getattr(error, "error_code", "conversion_failed")
    message = str(error)

    retryable: bool = error.retryable  # type: ignore[attr-defined]

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        if job.status == ConversionJobStatus.CANCELLED:
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


def poll_and_process_jobs(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> bool:
    """Pick up the next eligible job and invoke supervised processing."""
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

    process_job_supervised(job_id, config, poll_interval_seconds=poll_interval_seconds)
    return True


def run_worker(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> None:
    """Run the worker loop for polling, processing, and recovery."""
    logger.info("Starting conversion worker loop")
    last_recovery_check = 0.0
    while True:
        now = time.monotonic()
        if now - last_recovery_check >= config.worker_stale_job_check_seconds:
            recovered = recover_stale_running_jobs(config)
            if recovered:
                logger.warning("Recovered %d stale RUNNING jobs", recovered)
            last_recovery_check = now
        processed = poll_and_process_jobs(config, poll_interval_seconds=poll_interval_seconds)
        if not processed:
            time.sleep(poll_interval_seconds)
