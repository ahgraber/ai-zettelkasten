"""Per-job orchestration for conversion workers.

The worker delegates fetch → convert to the injected ``Orchestrator`` from
``aizk.conversion.wiring.worker.build_worker_runtime``.  The subprocess
boundary is preserved for crash isolation: the parent acquires the GPU
``ResourceGuard`` (when the dispatched converter has ``requires_gpu == True``),
spawns the child, supervises it, and unwinds the guard on return.  The child
builds its own ``WorkerRuntime`` (spawn mode re-imports), runs the
orchestrator, and serializes artifacts to the workspace for the parent-side
upload step.

Identity materialization (Source creation, aizk_uuid, source_ref, source_ref_hash)
is API-side.  The worker only enriches mutable Source metadata (``url``,
``normalized_url``, ``title``, ``content_type``, ``source_type``) and never
writes identity columns.
"""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time
from typing import Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    InlineHtmlRef,
    KarakeepBookmarkRef,
    SourceRefVariant,
    UrlRef,
    parse_source_ref,
)
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as SourceRecord
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.bookmark_utils import (
    BookmarkContentError,
    detect_content_type,
    detect_source_type,
    fetch_karakeep_bookmark,
    get_bookmark_source_url,
    validate_bookmark_content,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import build_output_config_snapshot, compute_markdown_hash
from aizk.conversion.utilities.paths import (
    OUTPUT_MARKDOWN_FILENAME,
    figure_dir,
    markdown_path,
    metadata_path,
)
from aizk.conversion.utilities.whitespace import normalize_whitespace
from aizk.conversion.wiring.worker import WorkerRuntime, build_worker_runtime
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
)
from aizk.conversion.workers.shutdown import is_shutdown_requested
from aizk.conversion.workers.supervision import _supervise_conversion_process
from aizk.conversion.workers.types import SupervisionResult, _utcnow
from aizk.conversion.workers.uploader import _upload_converted
from aizk.utilities.url_utils import normalize_url

logger = logging.getLogger(__name__)


_SOURCE_REF_FILENAME = "source_ref.json"


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


def _enrich_source_for_job(
    job_id: int, engine: Engine
) -> tuple[SourceRecord, SourceRefVariant]:
    """Populate the Source row's mutable metadata from the job's source_ref.

    Identity columns (``aizk_uuid``, ``source_ref``, ``source_ref_hash``,
    ``karakeep_id``) are NOT written — the API owns identity materialization.
    For ``KarakeepBookmarkRef`` the KaraKeep API is consulted to derive
    ``url``/``title``/``source_type``; for other refs, enrichment is derived
    from the ref fields themselves.
    """
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            raise JobDataIntegrityError(f"Job {job_id} missing during preflight")
        source = session.exec(
            select(SourceRecord).where(SourceRecord.aizk_uuid == job.aizk_uuid)
        ).one()
        source_ref_payload = dict(job.source_ref or source.source_ref)

    ref = parse_source_ref(source_ref_payload)

    if isinstance(ref, KarakeepBookmarkRef):
        karakeep_bookmark = fetch_karakeep_bookmark(ref.bookmark_id)
        if not karakeep_bookmark:
            raise FetchError(f"Bookmark {ref.bookmark_id} not found in KaraKeep")
        validate_bookmark_content(karakeep_bookmark)
        source_url = get_bookmark_source_url(karakeep_bookmark)
        updates: dict[str, object | None] = {
            "url": source_url,
            "normalized_url": normalize_url(source_url) if source_url else None,
            "title": karakeep_bookmark.title or source_url,
            "content_type": detect_content_type(karakeep_bookmark),
            "source_type": detect_source_type(source_url),
        }
    elif isinstance(ref, ArxivRef):
        abstract_url = f"https://arxiv.org/abs/{ref.arxiv_id}"
        updates = {
            "url": abstract_url,
            "normalized_url": normalize_url(abstract_url),
            "source_type": "arxiv",
            "content_type": "pdf",
        }
    elif isinstance(ref, GithubReadmeRef):
        repo_url = f"https://github.com/{ref.owner}/{ref.repo}"
        updates = {
            "url": repo_url,
            "normalized_url": normalize_url(repo_url),
            "source_type": "github",
            "content_type": "html",
        }
    elif isinstance(ref, UrlRef):
        updates = {
            "url": ref.url,
            "normalized_url": normalize_url(ref.url),
            "source_type": "other",
        }
    elif isinstance(ref, InlineHtmlRef):
        updates = {"content_type": "html", "source_type": "other"}
    else:
        updates = {}

    with Session(engine) as session:
        source = session.exec(
            select(SourceRecord).where(SourceRecord.aizk_uuid == job.aizk_uuid)
        ).one()
        for key, value in updates.items():
            if value is not None:
                setattr(source, key, value)
        source.updated_at = _utcnow()
        session.add(source)
        job_record = session.get(ConversionJob, job_id)
        if job_record and source.title:
            job_record.title = source.title
            job_record.updated_at = _utcnow()
            session.add(job_record)
        session.commit()
        session.refresh(source)

    return source, ref


def _pipeline_from_content_type(content_type: ContentType) -> Literal["html", "pdf"]:
    """Map a ``ContentType`` to the legacy ``pipeline_name`` used by the manifest."""
    if content_type is ContentType.PDF:
        return "pdf"
    return "html"


def _persist_artifacts(
    *,
    workspace: Path,
    artifacts: ConversionArtifacts,
    content_type: ContentType,
    fetched_at: dt.datetime,
    converter_name: str,
    config: ConversionConfig,
) -> None:
    """Write the in-memory artifacts + metadata.json to the workspace.

    The parent-side uploader reads this workspace layout unchanged from the
    pre-PR-7 flow.
    """
    markdown_text = normalize_whitespace(artifacts.markdown)
    markdown_file = markdown_path(workspace, OUTPUT_MARKDOWN_FILENAME)
    markdown_file.write_text(markdown_text)

    figures_root = figure_dir(workspace)
    figures_root.mkdir(parents=True, exist_ok=True)
    figure_filenames: list[str] = []
    for i, fig_bytes in enumerate(artifacts.figures):
        fig_name = f"figure-{i:03d}.png"
        (figures_root / fig_name).write_bytes(fig_bytes)
        figure_filenames.append(fig_name)

    markdown_hash = compute_markdown_hash(markdown_text)
    picture_description_enabled = config.is_picture_description_enabled()
    config_snapshot = build_output_config_snapshot(
        config,
        picture_description_enabled=picture_description_enabled,
    )

    metadata = {
        "pipeline_name": _pipeline_from_content_type(content_type),
        "fetched_at": fetched_at.isoformat(),
        "markdown_filename": OUTPUT_MARKDOWN_FILENAME,
        "figure_files": figure_filenames,
        "markdown_hash_xx64": markdown_hash,
        "docling_version": _docling_version(),
        "config_snapshot": config_snapshot,
        "converter_name": converter_name,
    }
    metadata_path(workspace).write_text(json.dumps(metadata, indent=2, sort_keys=False))


def _convert_job_artifacts(
    *,
    job_id: int,
    workspace: Path,
    source_ref_path: Path,
    status_queue: mp.Queue | None,
) -> None:
    """Run fetch + convert in the child process via the injected Orchestrator."""
    config = ConversionConfig()
    engine = get_engine(config.database_url)
    runtime = build_worker_runtime(config)

    _raise_if_cancelled(job_id, engine)
    ref_payload = json.loads(source_ref_path.read_text())
    ref = parse_source_ref(ref_payload)

    _report_status(status_queue, event="phase", message="preparing_input")
    fetched_at = _utcnow()

    _report_status(status_queue, event="phase", message="converting")
    _raise_if_cancelled(job_id, engine)

    # orchestrator.fetch() follows resolver hops and returns ConversionInput.
    # The fetch-chain populates ConversionInput.metadata with source_url for
    # adapters that need it (e.g. DoclingConverter.convert_html).
    conversion_input = runtime.orchestrator.fetch(ref)
    if not conversion_input.content:
        raise BookmarkContentUnavailableError(
            f"Fetcher returned empty content for job {job_id}"
        )
    _raise_if_cancelled(job_id, engine)
    converter = runtime.converter_registry.resolve(
        conversion_input.content_type, runtime.converter_name
    )
    artifacts = converter.convert(conversion_input)  # type: ignore[attr-defined]

    _raise_if_cancelled(job_id, engine)
    _persist_artifacts(
        workspace=workspace,
        artifacts=artifacts,
        content_type=conversion_input.content_type,
        fetched_at=fetched_at,
        converter_name=runtime.converter_name,
        config=config,
    )


def _process_job_subprocess(
    job_id: int,
    workspace_path: str,
    source_ref_path: str,
    status_queue: mp.Queue,
) -> None:
    """Subprocess entrypoint that reports conversion events to the parent."""
    import traceback as tb_mod

    # Lazy import to avoid loading docling (via DoclingConverter/convert_html/pdf)
    # in the parent process, which only needs ApiRuntime-equivalent wiring.
    from aizk.conversion.workers.converter import ConversionError

    os.setpgrp()  # Create new process group for cleanup of all descendants
    try:
        _convert_job_artifacts(
            job_id=job_id,
            workspace=Path(workspace_path),
            source_ref_path=Path(source_ref_path),
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
    source_ref_path: Path,
) -> tuple[mp.Process, mp.Queue]:
    """Start the conversion subprocess and return the process and status queue."""
    ctx = mp.get_context("spawn")
    status_queue: mp.Queue = ctx.Queue()
    process = ctx.Process(
        target=_process_job_subprocess,
        args=(job_id, str(workspace), str(source_ref_path), status_queue),
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
    source_ref_path: Path,
    poll_interval_seconds: float,
    timeout_seconds: float,
    is_cancelled_fn: Callable[[], bool],
    config: ConversionConfig,
) -> tuple[mp.Process, SupervisionResult, float | None]:
    """Spawn a conversion subprocess and supervise it.

    Caller is responsible for holding the GPU ``ResourceGuard`` around this
    call (via ``with runtime.gpu_guard:``) when the dispatched converter
    has ``requires_gpu == True``.  This function does NOT acquire the guard
    itself — GPU admission is a per-dispatch decision owned by the caller.
    """
    process, status_queue = _spawn_conversion_subprocess(
        job_id=job_id,
        workspace=workspace,
        source_ref_path=source_ref_path,
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
    return process, result, deadline


def process_job_supervised(
    job_id: int,
    config: ConversionConfig,
    runtime: WorkerRuntime,
    poll_interval_seconds: float = 2.0,
) -> None:
    """Run a supervised conversion attempt and upload artifacts on success.

    The parent process handles preflight, cancellation, timeout, uploads, and
    GPU-guard acquisition.  Conversion itself runs in a forked subprocess that
    builds its own ``WorkerRuntime`` and invokes the orchestrator's fetch →
    convert pipeline.
    """
    engine = get_engine(config.database_url)
    timeout_seconds = float(config.worker_job_timeout_seconds)

    if not _initialize_running_job(job_id, engine):
        return

    try:
        source, source_ref = _enrich_source_for_job(job_id, engine)
    except (FetchError, BookmarkContentError, JobDataIntegrityError) as exc:
        handle_job_error(job_id, exc, config)
        return
    except Exception as exc:
        handle_job_error(
            job_id, PreflightError(f"Job {job_id} preflight failed: {exc}"), config
        )
        return

    with tempfile.TemporaryDirectory() as tmpdirname:
        workspace = Path(tmpdirname)
        source_ref_path = workspace / _SOURCE_REF_FILENAME
        source_ref_path.write_text(source_ref.model_dump_json())

        # Admit the subprocess under the GPU guard iff the dispatched
        # converter declares requires_gpu. Today the only registered
        # converter (Docling) requires_gpu=True, so this branch is
        # always entered; the bypass branch is exercised by tests using
        # a fake converter with requires_gpu=False.
        if runtime.converter_requires_gpu():
            with runtime.gpu_guard:
                process, result, deadline = _spawn_and_supervise(
                    job_id=job_id,
                    workspace=workspace,
                    source_ref_path=source_ref_path,
                    poll_interval_seconds=poll_interval_seconds,
                    timeout_seconds=timeout_seconds,
                    is_cancelled_fn=lambda: _is_job_cancelled(job_id, engine),
                    config=config,
                )
        else:
            process, result, deadline = _spawn_and_supervise(
                job_id=job_id,
                workspace=workspace,
                source_ref_path=source_ref_path,
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
                ConversionSubprocessError(
                    f"Job {job_id} subprocess exited with code {process.exitcode}"
                ),
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
