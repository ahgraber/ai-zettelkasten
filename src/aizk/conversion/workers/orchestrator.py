"""Per-job orchestration for conversion workers."""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as SourceRecord
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import compute_markdown_hash
from aizk.conversion.utilities.paths import (
    OUTPUT_MARKDOWN_FILENAME,
    metadata_path,
)
from aizk.conversion.utilities.whitespace import normalize_whitespace
from aizk.conversion.workers.errors import (
    ConversionCancelledError,
    ConversionSubprocessError,
    ConversionTimeoutError,
    JobDataIntegrityError,
    ReportedChildError,
)
from aizk.conversion.workers.shutdown import is_shutdown_requested
from aizk.conversion.workers.supervision import _supervise_conversion_process
from aizk.conversion.workers.types import SupervisionResult, _utcnow
from aizk.conversion.workers.uploader import _upload_converted

if TYPE_CHECKING:
    from aizk.conversion.wiring.worker import WorkerRuntime

logger = logging.getLogger(__name__)


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


def _get_source_ref(job_id: int, engine: Engine):
    """Read and deserialize source_ref from the job record."""
    from pydantic import TypeAdapter

    from aizk.conversion.core.source_ref import SourceRef

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            raise JobDataIntegrityError(f"Job {job_id} missing during preflight")
        if not job.source_ref:
            raise JobDataIntegrityError(f"Job {job_id} has no source_ref")
        return TypeAdapter(SourceRef).validate_python(json.loads(job.source_ref))


def _enrich_source_metadata(
    aizk_uuid: str,
    terminal_ref,  # SourceRef
    content_type_str: str | None,
    engine,
) -> None:
    """Best-effort update of mutable Source metadata. Logs on failure, never raises."""
    from aizk.conversion.core.types import SOURCE_TYPE_BY_KIND

    try:
        source_type = SOURCE_TYPE_BY_KIND.get(terminal_ref.kind, "other")
        with Session(engine) as session:
            source = session.exec(select(SourceRecord).where(SourceRecord.aizk_uuid == aizk_uuid)).one_or_none()
            if source is None:
                logger.warning(
                    "Source row not found for aizk_uuid=%s during enrichment",
                    aizk_uuid,
                )
                return
            # ONLY write mutable metadata columns — never write identity columns
            source.source_type = source_type
            if content_type_str:
                source.content_type = content_type_str
            source.updated_at = _utcnow()
            session.add(source)
            session.commit()
    except Exception:
        logger.exception(
            "Source enrichment failed for aizk_uuid=%s (best-effort; job proceeds)",
            aizk_uuid,
        )


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


def _process_job_subprocess(
    job_id: int,
    workspace_path: str,
    source_ref_json: str,
    status_queue: mp.Queue,
) -> None:
    """Subprocess entrypoint — builds its own runtime and runs orchestrator.process_with_provenance()."""
    import traceback as tb_mod

    from pydantic import TypeAdapter

    from aizk.conversion.core.source_ref import SourceRef as _SourceRef
    from aizk.conversion.wiring.worker import build_worker_runtime

    os.setpgrp()  # Create new process group for cleanup of all descendants

    def _do_convert():
        config = ConversionConfig()
        engine = get_engine(config.database_url)

        source_ref = TypeAdapter(_SourceRef).validate_python(json.loads(source_ref_json))
        _raise_if_cancelled(job_id, engine)

        runtime = build_worker_runtime(config)
        converter_name = config.worker_converter_name

        _report_status(status_queue, event="phase", message="preparing_input")

        result = runtime.orchestrator.process_with_provenance(source_ref, converter_name)

        _report_status(status_queue, event="phase", message="converting")

        workspace = Path(workspace_path)

        # Write markdown
        markdown_text = normalize_whitespace(result.artifacts.markdown)
        markdown_file = workspace / OUTPUT_MARKDOWN_FILENAME
        markdown_file.write_text(markdown_text)
        markdown_hash = compute_markdown_hash(markdown_text)

        # Copy figures from converter's tempdir to workspace
        figure_file_names = []
        for fig in result.artifacts.figures:
            if isinstance(fig, Path) and fig.exists():
                dest = workspace / fig.name
                shutil.copy2(fig, dest)
                figure_file_names.append(fig.name)

        # Config snapshot from the converter
        converter = runtime.orchestrator._resolve_converter(result.conversion_input.content_type, converter_name)
        adapter_snapshot = converter.config_snapshot() if hasattr(converter, "config_snapshot") else {}

        pipeline_name = result.conversion_input.content_type.value  # "pdf" or "html"
        docling_ver = result.artifacts.metadata.get("docling_version", _docling_version())

        # fetched_at: use now() as fallback
        fetched_at = dt.datetime.now(dt.timezone.utc)

        metadata = {
            "pipeline_name": pipeline_name,
            "fetched_at": fetched_at.isoformat(),
            "markdown_filename": OUTPUT_MARKDOWN_FILENAME,
            "figure_files": figure_file_names,
            "markdown_hash_xx64": markdown_hash,
            "docling_version": docling_ver,
            "config_snapshot": {
                "converter_name": converter_name,
                **adapter_snapshot,
            },
            # Extra fields for parent enrichment and v2 manifest
            "terminal_ref": result.terminal_ref.model_dump(mode="json"),
            "content_type": pipeline_name,
        }
        metadata_file = metadata_path(workspace)
        metadata_file.write_text(json.dumps(metadata, indent=2, sort_keys=False))

        _report_status(status_queue, event="completed", message="conversion completed")

    try:
        _do_convert()
    except ConversionCancelledError:
        _report_status(status_queue, event="cancelled", message="conversion cancelled")
    except Exception as exc:
        error_code = getattr(exc, "error_code", "conversion_failed")
        retryable = getattr(exc, "retryable", True)
        _report_status(
            status_queue,
            event="failed",
            message=str(exc),
            error_code=error_code,
            retryable=retryable,
            traceback_text=tb_mod.format_exc(),
        )
        raise


def _spawn_conversion_subprocess(
    *,
    job_id: int,
    workspace: Path,
    source_ref_json: str,
) -> tuple[mp.Process, mp.Queue]:
    """Start the conversion subprocess and return the process and status queue."""
    ctx = mp.get_context("spawn")
    status_queue: mp.Queue = ctx.Queue()
    process = ctx.Process(
        target=_process_job_subprocess,
        args=(job_id, str(workspace), source_ref_json, status_queue),
        daemon=True,
    )
    process.start()
    return process, status_queue


def _spawn_and_supervise(
    *,
    job_id: int,
    workspace: Path,
    source_ref_json: str,
    poll_interval_seconds: float,
    timeout_seconds: float,
    is_cancelled_fn: Callable[[], bool],
    config: ConversionConfig,
    resource_guard,
    requires_gpu: bool,
) -> tuple[mp.Process, SupervisionResult, float | None]:
    """Spawn and supervise; acquire resource_guard only if requires_gpu."""
    from contextlib import nullcontext

    guard_ctx = resource_guard if (requires_gpu and resource_guard is not None) else nullcontext()

    with guard_ctx:
        process, status_queue = _spawn_conversion_subprocess(
            job_id=job_id,
            workspace=workspace,
            source_ref_json=source_ref_json,
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
    runtime: "WorkerRuntime | None" = None,
    *,
    poll_interval_seconds: float = 2.0,
) -> None:
    """Run a supervised conversion attempt and upload artifacts on success.

    The parent process handles preflight, cancellation, timeout, and uploads.
    """
    from aizk.conversion.wiring.worker import WorkerRuntime, build_worker_runtime

    if runtime is None:
        runtime = build_worker_runtime(config)

    engine = get_engine(config.database_url)
    timeout_seconds = float(config.worker_job_timeout_seconds)

    if not _initialize_running_job(job_id, engine):
        return

    try:
        source_ref = _get_source_ref(job_id, engine)
    except JobDataIntegrityError as exc:
        handle_job_error(job_id, exc, config)
        return

    converter_name = config.worker_converter_name
    requires_gpu = runtime.capabilities.converter_requires_gpu(converter_name)

    with tempfile.TemporaryDirectory() as tmpdirname:
        workspace = Path(tmpdirname)
        source_ref_json = source_ref.model_dump_json()

        process, result, deadline = _spawn_and_supervise(
            job_id=job_id,
            workspace=workspace,
            source_ref_json=source_ref_json,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            is_cancelled_fn=lambda: _is_job_cancelled(job_id, engine),
            config=config,
            resource_guard=runtime.resource_guard,
            requires_gpu=requires_gpu,
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

        # Best-effort Source enrichment from metadata written by subprocess
        metadata_file = workspace / "metadata.json"
        if metadata_file.exists():
            try:
                metadata = json.loads(metadata_file.read_text())
                terminal_ref_data = metadata.get("terminal_ref")
                content_type_str = metadata.get("content_type")
                if terminal_ref_data:
                    from pydantic import TypeAdapter

                    from aizk.conversion.core.source_ref import SourceRef as _SourceRef

                    terminal_ref = TypeAdapter(_SourceRef).validate_python(terminal_ref_data)

                    with Session(engine) as session:
                        job_rec = session.get(ConversionJob, job_id)
                        if job_rec:
                            aizk_uuid = job_rec.aizk_uuid
                            _enrich_source_metadata(aizk_uuid, terminal_ref, content_type_str, engine)
            except Exception:
                logger.exception("Failed to read terminal_ref for enrichment; job proceeds")

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
