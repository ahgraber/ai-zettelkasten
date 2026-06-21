"""Per-job conversion subprocess: spawn, supervise, cancellation, and IPC."""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import time
from typing import TYPE_CHECKING, Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.processing.errors import ConversionCancelledError
from aizk.conversion.processing.supervision import _supervise_conversion_process
from aizk.conversion.processing.types import SubprocessMetadata, SupervisionResult, select_source_title
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.dotenv import load_process_dotenv_once
from aizk.conversion.utilities.hashing import compute_markdown_hash
from aizk.conversion.utilities.paths import (
    OUTPUT_MARKDOWN_FILENAME,
    metadata_path,
)
from aizk.conversion.utilities.whitespace import normalize_whitespace
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine

if TYPE_CHECKING:
    import threading


logger = logging.getLogger(__name__)


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


def _process_job_subprocess(
    job_id: int,
    workspace_path: str,
    source_ref_json: str,
    status_queue: mp.Queue,
) -> None:
    """Subprocess entrypoint — builds its own runtime and runs the coordinator.process_with_provenance()."""
    import traceback as tb_mod

    from pydantic import TypeAdapter

    from aizk.conversion.core.source_ref import SourceRef as _SourceRef
    from aizk.conversion.wiring.worker import build_worker_runtime

    os.setpgrp()  # Create new process group for cleanup of all descendants

    def _do_convert():
        load_process_dotenv_once()
        config = ConversionConfig()
        engine = get_engine(DatabaseConfig().database_url)

        source_ref = TypeAdapter(_SourceRef).validate_python(json.loads(source_ref_json))
        _raise_if_cancelled(job_id, engine)

        runtime = build_worker_runtime(config)
        converter_name = config.worker_converter_name

        _report_status(status_queue, event="phase", message="preparing_input")

        result = runtime.coordinator.process_with_provenance(source_ref, converter_name)

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

        pipeline_name = result.conversion_input.content_type.value  # "pdf" or "html"
        # DoclingConverter populates `docling_version` on every artifacts.metadata; the
        # default protects unknown future converters from KeyError without a runtime probe.
        docling_ver = result.artifacts.metadata.get("docling_version", "unknown")

        fetched_at = dt.datetime.now(dt.timezone.utc)

        final_source_meta = result.conversion_input.source_meta
        source_title = select_source_title(
            result.artifacts.document_title,
            final_source_meta.resolver_title,
        )

        from aizk.conversion.processing.types import SourceMetaFields

        subprocess_meta = SubprocessMetadata(
            pipeline_name=pipeline_name,
            terminal_ref=result.terminal_ref.model_dump(mode="json"),
            content_type=pipeline_name,
            markdown_filename=OUTPUT_MARKDOWN_FILENAME,
            figure_files=figure_file_names,
            markdown_hash_xx64=markdown_hash,
            docling_version=docling_ver,
            config_snapshot={
                "converter_name": result.converter_name,
                **result.config_snapshot,
            },
            fetched_at=fetched_at.isoformat(),
            source_meta=SourceMetaFields.from_source_metadata(final_source_meta),
            document_title=result.artifacts.document_title,
            source_title=source_title,
        )
        metadata_file = metadata_path(workspace)
        metadata_file.write_text(subprocess_meta.model_dump_json(indent=2))

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
    on_phase_event=None,
    on_spawn: Callable[[mp.Process], None] | None = None,
    terminate_event: "threading.Event | None" = None,
    shutdown_requested_fn: Callable[[], bool] | None = None,
) -> tuple[mp.Process, SupervisionResult, float | None]:
    """Spawn and supervise; acquire resource_guard only if requires_gpu.

    ``on_spawn``, when supplied, is invoked once with the freshly spawned
    subprocess immediately before supervision begins blocking. The pipeline
    runner adapter uses it to register the live process so its cancellation
    hook can terminate the process group while supervision is still running.

    ``terminate_event``, when supplied, is forwarded to the supervision loop so
    an out-of-band owner can *signal* termination (the supervision loop, the
    single owner of the Process, performs the actual terminate/join). The
    pipeline runner adapter uses this seam to drive drain/cancel.

    ``shutdown_requested_fn`` defaults to ``None``: the runner owns drain (it
    cancels in-flight units via ``terminate_event`` on shutdown), so the
    supervision loop's module-global shutdown-drain branch is dead on the
    runner path and the adapter passes ``None``. The parameter exists so the
    supervision loop never depends on any module-global shutdown state.
    """
    from contextlib import nullcontext

    guard_ctx = resource_guard if (requires_gpu and resource_guard is not None) else nullcontext()

    with guard_ctx:
        process, status_queue = _spawn_conversion_subprocess(
            job_id=job_id,
            workspace=workspace,
            source_ref_json=source_ref_json,
        )

        if on_spawn is not None:
            on_spawn(process)

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
            shutdown_requested_fn=shutdown_requested_fn,
            drain_timeout_seconds=float(config.worker_drain_timeout_seconds),
            on_phase_event=on_phase_event,
            terminate_event=terminate_event,
        )
    return process, result, deadline
