"""Per-job orchestration for conversion workers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime as dt
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import time
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from aizk.conversion.core.errors import EgressPolicyError
from aizk.conversion.datamodel.events import (
    record_source_event,
)
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as SourceRecord
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.dotenv import load_process_dotenv_once
from aizk.conversion.utilities.hashing import compute_markdown_hash
from aizk.conversion.utilities.paths import (
    OUTPUT_MARKDOWN_FILENAME,
    metadata_path,
)
from aizk.conversion.utilities.whitespace import normalize_whitespace
from aizk.conversion.workers.errors import (
    ConversionCancelledError,
    JobDataIntegrityError,
)
from aizk.conversion.workers.supervision import _supervise_conversion_process
from aizk.conversion.workers.types import SubprocessMetadata, SupervisionResult, _utcnow, select_source_title

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


def _write_source_enrichment(
    subprocess_meta: SubprocessMetadata,
    aizk_uuid: str,
    engine,
    *,
    job_id: int,
    attempt: int,
) -> None:
    """Best-effort update of Source row from SubprocessMetadata. Logs on failure, never raises.

    Writes url, normalized_url, title, source_type, and content_type from the
    subprocess IPC payload. Failure here does not affect manifest, ConversionOutput,
    or job completion — Source is an advisory cache only.

    A ``source_enriched`` event is appended to the event log regardless of
    whether the Source UPDATE succeeded or failed, scoped to the originating
    ``(job_id, attempt)``. The audit captures what the worker attempted and
    the outcome; the Source mutation itself remains advisory.
    """
    from uuid import UUID as _UUID

    from pydantic import TypeAdapter

    from aizk.conversion.core.source_ref import SourceRef as _SourceRef
    from aizk.conversion.core.types import SOURCE_TYPE_BY_KIND

    uuid_obj = aizk_uuid if isinstance(aizk_uuid, _UUID) else _UUID(aizk_uuid)

    columns_attempted: list[str] = ["url", "normalized_url", "title", "source_type"]
    if subprocess_meta.content_type:
        columns_attempted.append("content_type")

    update_succeeded = False
    failure_reason: str | None = None

    try:
        terminal_ref: _SourceRef = TypeAdapter(_SourceRef).validate_python(subprocess_meta.terminal_ref)
        source_type = SOURCE_TYPE_BY_KIND.get(terminal_ref.kind, "other")
        source_meta = subprocess_meta.source_meta.to_source_metadata()

        with Session(engine) as session:
            source = session.exec(select(SourceRecord).where(SourceRecord.aizk_uuid == uuid_obj)).one_or_none()
            if source is None:
                logger.warning(
                    "Source row not found for aizk_uuid=%s during enrichment",
                    aizk_uuid,
                )
                failure_reason = "source_row_not_found"
            else:
                # Only write mutable metadata columns — never write identity columns.
                source.url = source_meta.source_url
                source.normalized_url = source_meta.normalized_url
                source.title = subprocess_meta.source_title
                source.source_type = source_type
                if subprocess_meta.content_type:
                    source.content_type = subprocess_meta.content_type
                source.updated_at = _utcnow()
                session.add(source)
                session.commit()
                update_succeeded = True
    except Exception as exc:
        logger.exception(
            "Source enrichment failed for aizk_uuid=%s (best-effort; job proceeds)",
            aizk_uuid,
        )
        failure_reason = str(exc)

    # Audit the attempt regardless of outcome. The event is best-effort: a
    # failure here is logged but does not propagate (Source enrichment is
    # advisory).
    try:
        with Session(engine) as event_session:
            record_source_event(
                event_session,
                job_id=job_id,
                aizk_uuid=uuid_obj,
                attempt=attempt,
                columns_written=columns_attempted,
                update_succeeded=update_succeeded,
                failure_reason=failure_reason,
            )
            event_session.commit()
    except Exception:
        logger.exception(
            "Failed to record source_enriched event for aizk_uuid=%s (best-effort)",
            aizk_uuid,
        )


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
        load_process_dotenv_once()
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

        from aizk.conversion.workers.types import SourceMetaFields

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


_EGRESS_POLICY_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "deny_list",
        "disallowed_scheme",
        "redirect_egress_violation",
        "dns_timeout",
        "workspace_escape",
        "egress_policy_violation",
    }
)


@dataclass(frozen=True)
class JobErrorDetails:
    """Scrubbed, persistence-ready failure details extracted from an exception.

    Carries exactly the values the ``ConversionStageHandler.finalize`` adapter
    writes to ``ConversionJob.error_*`` and the ``FailedPayload`` event: the
    ``error_code``, the egress-scrubbed ``error_message`` and ``error_detail``,
    the retry disposition, and the optional ``last_phase``. Egress-policy errors
    are already scrubbed here
    (``error_message`` is the bare code, ``error_detail`` is ``None``) so a
    rejected URL/IP never reaches the caller, let alone durable storage.
    """

    error_code: str
    error_message: str
    error_detail: str | None
    retryable: bool
    last_phase: str | None


def classify_job_error(error: Exception) -> JobErrorDetails:
    """Extract the scrubbed, persistence-ready failure details from ``error``.

    The single source of truth for how a conversion exception maps to the
    durable failure fields, called by the ``ConversionStageHandler.finalize``
    adapter so the error_code, retry decision, and the egress scrub stay
    consistent.

    Egress-policy errors carry rejected destinations (URLs, IPs) in their
    message/traceback. They are scrubbed here on two paths:

    1. Direct ``isinstance(error, EgressPolicyError)`` — error raised in this
       process (e.g., from ``_get_source_ref``).
    2. ``ReportedChildError`` whose ``error_code`` is one of the egress-policy
       codes — error raised in the conversion subprocess and reported up.

    Both paths set ``error_message`` to the bare ``error_code`` and drop
    ``error_detail`` (which would otherwise carry the destination via the
    traceback). The full detail is already captured by the enforcement-site
    WARNING logs in egress.py / egress_fetch.py / paths.py.
    """
    error_code = getattr(error, "error_code", "conversion_failed")
    error_detail = getattr(error, "traceback", None)

    # Default to retryable=True for exceptions that lack the conversion-error
    # contract (plain OSError, KeyError, etc. that may leak from the upload-retry
    # arm). The unknown-exception default matches FetchError's policy: when in
    # doubt, retry rather than mark permanent.
    retryable: bool = bool(getattr(error, "retryable", True))

    is_egress_policy = isinstance(error, EgressPolicyError) or error_code in _EGRESS_POLICY_ERROR_CODES
    if is_egress_policy:
        message = error_code
        error_detail = None
    else:
        message = str(error)

    last_phase = getattr(error, "last_phase", None)
    return JobErrorDetails(
        error_code=error_code,
        error_message=message,
        error_detail=error_detail,
        retryable=retryable,
        last_phase=last_phase,
    )
