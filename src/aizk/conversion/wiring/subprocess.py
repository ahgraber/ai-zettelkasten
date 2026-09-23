"""Conversion child entry — the spawned per-job process that builds its own runtime and converts."""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import shutil
from typing import Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session

from aizk.conversion.core.hashing import compute_markdown_hash
from aizk.conversion.core.source_ref import SourceRef
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.processing.errors import ConversionCancelledError
from aizk.conversion.processing.types import (
    SourceMetaFields,
    SubprocessMetadata,
    select_source_title,
)
from aizk.conversion.processing.whitespace import normalize_whitespace
from aizk.conversion.settings import ConversionConfig
from aizk.conversion.storage.paths import (
    OUTPUT_MARKDOWN_FILENAME,
    metadata_path,
)
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.service.bootstrap import load_process_dotenv_once, set_process_title
from aizk.storage.settings import ObjectStorageSettings
from aizk.utilities.clock import utcnow

logger = logging.getLogger(__name__)


def _raise_if_cancelled(job_id: int, engine: Engine) -> None:
    """Raise if the job status has been marked as cancelled."""
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if job and job.status == ConversionJobStatus.CANCELLED:
            raise ConversionCancelledError(f"Job {job_id} cancelled")


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
    os.setpgrp()  # Create new process group for cleanup of all descendants
    set_process_title(f"docling-convert:{job_id}")

    import traceback as tb_mod

    from pydantic import TypeAdapter

    from aizk.conversion.wiring.worker import build_worker_runtime

    def _do_convert():
        load_process_dotenv_once()
        config = ConversionConfig()
        object_storage = ObjectStorageSettings()
        engine = get_engine(DatabaseConfig().database_url)

        source_ref = TypeAdapter(SourceRef).validate_python(json.loads(source_ref_json))
        _raise_if_cancelled(job_id, engine)

        runtime = build_worker_runtime(config, object_storage=object_storage)
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

        fetched_at = utcnow()

        final_source_meta = result.conversion_input.source_meta
        source_title = select_source_title(
            result.artifacts.document_title,
            final_source_meta.resolver_title,
        )

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
