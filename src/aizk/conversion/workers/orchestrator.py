"""Per-job orchestration for conversion workers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import datetime as dt
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from aizk.conversion.datamodel.bookmark import Bookmark as BookmarkRecord
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.db import get_engine
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
    markdown_path,
    metadata_path,
)
from aizk.conversion.utilities.whitespace import normalize_whitespace
from aizk.conversion.workers.converter import (
    ConversionError,
    convert_html,
    convert_pdf,
)
from aizk.conversion.workers.errors import (
    ConversionCancelledError,
    ConversionSubprocessError,
    ConversionTimeoutError,
    JobDataIntegrityError,
    PreflightError,
    ReportedChildError,
)
from aizk.conversion.workers.fetcher import (
    BookmarkContentUnavailableError,
    FetchError,
    fetch_arxiv,
    fetch_github_readme,
    fetch_karakeep_asset,
)
from aizk.conversion.workers.shutdown import is_shutdown_requested
from aizk.conversion.workers.supervision import _supervise_conversion_process
from aizk.conversion.workers.types import (
    ConversionArtifacts,
    ConversionInput,
    SupervisionResult,
    _utcnow,
)
from aizk.conversion.workers.uploader import _upload_converted
from aizk.utilities.async_utils import run_async
from aizk.utilities.url_utils import normalize_url
from karakeep_client.models import Bookmark as KarakeepBookmark

logger = logging.getLogger(__name__)

# Module-level GPU semaphore.  Limits concurrent conversion subprocesses
# to prevent GPU OOM when multiple jobs run in parallel.
_gpu_semaphore: threading.Semaphore | None = None


def configure_gpu_semaphore(gpu_concurrency: int) -> None:
    """Set the GPU concurrency limit.  Called once at worker startup."""
    global _gpu_semaphore  # noqa: PLW0603
    _gpu_semaphore = threading.Semaphore(gpu_concurrency)


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
    traceback_text: str | None = None,
) -> None:
    """Send a structured event from the subprocess to the parent."""
    if not status_queue:
        return
    payload: dict[str, str] = {"event": event, "message": message}
    if error_code:
        payload["error_code"] = error_code
    if retryable is not None:
        payload["retryable"] = "true" if retryable else "false"
    if traceback_text:
        payload["traceback"] = traceback_text
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


def _process_job_subprocess(
    job_id: int,
    workspace_path: str,
    karakeep_payload_path: str,
    status_queue: mp.Queue,
) -> None:
    """Subprocess entrypoint that reports conversion events to the parent."""
    import traceback as tb_mod

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
            traceback_text=tb_mod.format_exc(),
        )
        raise
    except Exception as exc:
        _report_status(
            status_queue,
            event="failed",
            message=str(exc),
            error_code="conversion_failed",
            retryable=True,
            traceback_text=tb_mod.format_exc(),
        )
        raise


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


def _spawn_and_supervise(
    *,
    job_id: int,
    workspace: Path,
    payload_path: Path,
    poll_interval_seconds: float,
    timeout_seconds: float,
    is_cancelled_fn: Callable[[], bool],
    config: ConversionConfig,
) -> tuple[mp.Process, SupervisionResult, float | None]:
    """Spawn a conversion subprocess under the GPU semaphore and supervise it."""
    sem = _gpu_semaphore
    if sem is not None:
        sem.acquire()
    try:
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
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=poll_interval_seconds,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            is_cancelled_fn=is_cancelled_fn,
            shutdown_requested_fn=is_shutdown_requested,
            drain_timeout_seconds=float(config.worker_drain_timeout_seconds),
        )
    finally:
        if sem is not None:
            sem.release()
    return process, result, deadline


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

        process, result, deadline = _spawn_and_supervise(
            job_id=job_id,
            workspace=workspace,
            payload_path=payload_path,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            is_cancelled_fn=lambda: _is_job_cancelled(job_id, engine),
            config=config,
        )

        if result.timed_out:
            handle_job_error(
                job_id,
                ConversionTimeoutError(
                    f"Job {job_id} exceeded its runtime during {result.last_phase}",
                    result.last_phase,
                ),
                config,
            )
            return

        if result.shutdown_terminated:
            handle_job_error(
                job_id,
                ConversionTimeoutError(
                    f"Job {job_id} terminated during shutdown drain in {result.last_phase}",
                    result.last_phase,
                ),
                config,
            )
            return

        if result.cancelled:
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
                    traceback=result.reported_error.get("traceback"),
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
    error_detail = getattr(error, "traceback", None)

    retryable: bool = error.retryable  # type: ignore[attr-defined]

    logger.error(
        "Job %s failed: %s (code=%s, retryable=%s)",
        job_id,
        message,
        error_code,
        retryable,
        extra={"job_id": job_id, "error_code": error_code, "error_detail": error_detail},
    )

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
        job.error_detail = error_detail
        job.last_error_at = now
        job.updated_at = now
        session.add(job)
        session.commit()
