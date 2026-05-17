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
import time
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from aizk.conversion.core.errors import EgressPolicyError
from aizk.conversion.datamodel.events import (
    ClaimedPayload,
    ConversionEventKind,
    FailedPayload,
    UploadPendingPayload,
    record_phase_event,
    record_source_event,
    record_transition,
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
    read_text_nofollow,
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
from aizk.conversion.workers.types import SubprocessMetadata, SupervisionResult, _utcnow, select_source_title
from aizk.conversion.workers.uploader import _execute_upload, _prepare_upload

if TYPE_CHECKING:
    from aizk.conversion.wiring.worker import WorkerRuntime

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

    columns_attempted: list[str] = ["url", "normalized_url", "title", "source_type"]
    if subprocess_meta.content_type:
        columns_attempted.append("content_type")

    update_succeeded = False
    failure_reason: str | None = None

    try:
        terminal_ref: _SourceRef = TypeAdapter(_SourceRef).validate_python(subprocess_meta.terminal_ref)
        source_type = SOURCE_TYPE_BY_KIND.get(terminal_ref.kind, "other")
        source_meta = subprocess_meta.source_meta.to_source_metadata()

        uuid_obj = aizk_uuid if isinstance(aizk_uuid, _UUID) else _UUID(aizk_uuid)
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
        uuid_for_event = aizk_uuid if isinstance(aizk_uuid, _UUID) else _UUID(aizk_uuid)
        with Session(engine) as event_session:
            record_source_event(
                event_session,
                job_id=job_id,
                aizk_uuid=uuid_for_event,
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


def _initialize_running_job(job_id: int, engine: Engine) -> bool:
    """Ensure the job is in RUNNING state before processing.

    Re-entrant claim path: in the normal worker flow ``claim_next_job`` has
    already transitioned the job to RUNNING and emitted its ``claimed``
    event, so this function is a no-op. For direct callers (tests, CLI) or
    recovery cases where the job arrives in a non-RUNNING state, this
    function transitions it to RUNNING, increments the attempt counter,
    and emits a ``claimed`` event distinct from the ``claim_next_job`` path.
    """
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return False
        if job.status in {ConversionJobStatus.SUCCEEDED, ConversionJobStatus.CANCELLED}:
            return False
        if job.status != ConversionJobStatus.RUNNING:
            now = _utcnow()
            job.started_at = now
            job.attempts += 1
            job.updated_at = now
            record_transition(
                session,
                job,
                to_status=ConversionJobStatus.RUNNING,
                kind=ConversionEventKind.CLAIMED,
                attempt=job.attempts,
                payload=ClaimedPayload(claimed_at=now, worker_pid=os.getpid()),
            )
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
            on_phase_event=on_phase_event,
        )
    return process, result, deadline


def process_job_supervised(  # noqa: C901
    job_id: int,
    config: ConversionConfig,
    runtime: "WorkerRuntime | None" = None,
    *,
    poll_interval_seconds: float = 2.0,
) -> None:
    """Run a supervised conversion attempt and upload artifacts on success.

    The parent process handles preflight, cancellation, timeout, and uploads.
    """
    from aizk.conversion.wiring.worker import build_worker_runtime

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

    # Snapshot (aizk_uuid, attempt) once at supervision entry. The subprocess
    # does not see the job's attempt counter, so the parent must capture it
    # before phase events start arriving on the queue.
    with Session(engine) as snap_session:
        snap_job = snap_session.get(ConversionJob, job_id)
        if snap_job is None:
            return
        attempt_snapshot = snap_job.attempts
        aizk_uuid_snapshot = snap_job.aizk_uuid

    def _on_phase_event(phase: str, reported_at: dt.datetime) -> None:
        """Persist one phase event per subprocess report; best-effort."""
        try:
            with Session(engine) as phase_session:
                record_phase_event(
                    phase_session,
                    job_id=job_id,
                    aizk_uuid=aizk_uuid_snapshot,
                    attempt=attempt_snapshot,
                    current_status=ConversionJobStatus.RUNNING,
                    phase=phase,
                    reported_at=reported_at,
                )
                phase_session.commit()
        except Exception:
            logger.warning(
                "Failed to commit phase event for job %s phase %r (best-effort)",
                job_id,
                phase,
                exc_info=True,
            )

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
            on_phase_event=_on_phase_event,
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

        # Best-effort Source enrichment from metadata written by subprocess.
        # Uses typed SubprocessMetadata so schema drift fails loudly rather than
        # silently dropping fields. Failure here does not affect manifest or output.
        # We also pull subprocess_meta into outer scope so the UPLOAD_PENDING
        # transition's payload can carry the content_hash when available.
        subprocess_meta: SubprocessMetadata | None = None
        metadata_file = metadata_path(workspace)
        if metadata_file.exists():
            try:
                from pydantic import ValidationError

                from aizk.conversion.workers.errors import SubprocessMetadataInvalid

                raw = read_text_nofollow(metadata_file)
                try:
                    subprocess_meta = SubprocessMetadata.model_validate_json(raw)
                except ValidationError as exc:
                    raise SubprocessMetadataInvalid(
                        f"metadata.json failed SubprocessMetadata validation: {exc}"
                    ) from exc

                with Session(engine) as session:
                    job_rec = session.get(ConversionJob, job_id)
                    if job_rec:
                        _write_source_enrichment(
                            subprocess_meta,
                            str(job_rec.aizk_uuid),
                            engine,
                            job_id=job_id,
                            attempt=job_rec.attempts,
                        )
            except Exception:
                logger.exception("Failed to read SubprocessMetadata for enrichment; job proceeds")

        content_hash = subprocess_meta.markdown_hash_xx64 if subprocess_meta else None
        with Session(engine) as session:
            job_record = session.get(ConversionJob, job_id)
            if job_record:
                job_record.updated_at = _utcnow()
                record_transition(
                    session,
                    job_record,
                    to_status=ConversionJobStatus.UPLOAD_PENDING,
                    kind=ConversionEventKind.UPLOAD_PENDING,
                    attempt=job_record.attempts,
                    payload=UploadPendingPayload(content_hash=content_hash),
                )
                session.commit()

        # Local prep (read metadata, generate + write manifest) runs exactly
        # once outside the retry loop. It is not idempotent against itself
        # — save_manifest uses O_EXCL — and re-running it would deterministically
        # fail attempt 2 with FileExistsError after a transient S3 failure on
        # attempt 1. The retry loop only wraps the S3 PUTs, which are
        # idempotent on their key.
        try:
            upload_plan = _prepare_upload(job_id, workspace, config)
        except Exception as exc:
            handle_job_error(job_id, exc, config)
            return
        if upload_plan is None:
            return

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
                _execute_upload(upload_plan, job_id, config)
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


def handle_job_error(job_id: int, error: Exception, config: ConversionConfig) -> None:
    """Persist job failure details and compute retryability.

    Retry decision uses the `retryable` class attribute on every exception class.
    """
    engine = get_engine(config.database_url)
    now = _utcnow()

    error_code = getattr(error, "error_code", "conversion_failed")
    error_detail = getattr(error, "traceback", None)

    # Default to retryable=True for exceptions that lack the conversion-error
    # contract (plain OSError, KeyError, etc. that may leak from the upload-retry
    # arm). The unknown-exception default matches FetchError's policy: when in
    # doubt, retry rather than mark permanent.
    retryable: bool = bool(getattr(error, "retryable", True))

    # EgressPolicyError messages carry rejected destinations (URLs, IPs) that
    # must not be echoed into persisted output. Sanitize on two paths:
    #   1. Direct `isinstance(error, EgressPolicyError)` — error raised in this
    #      process (e.g., from `_get_source_ref`).
    #   2. `ReportedChildError` whose `error_code` is one of the egress-policy
    #      codes — error raised in the conversion subprocess and reported up.
    # Both paths set `error_message` to the error_code only AND drop
    # `error_detail` (which would carry the destination via the traceback
    # string). The full detail is already captured by the enforcement-site
    # WARNING logs in egress.py / egress_fetch.py / paths.py.
    is_egress_policy = isinstance(error, EgressPolicyError) or error_code in _EGRESS_POLICY_ERROR_CODES
    if is_egress_policy:
        message = error_code
        error_detail = None
    else:
        message = str(error)

    logger.error(
        "Job %s failed: %s (code=%s, retryable=%s)",
        job_id,
        message,
        error_code,
        retryable,
        extra={"job_id": job_id, "error_code": error_code, "error_detail": error_detail},
    )

    last_phase = getattr(error, "last_phase", None)

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        if job.status == ConversionJobStatus.CANCELLED:
            return
        if retryable:
            delay = config.retry_base_delay_seconds * (2**job.attempts)
            job.earliest_next_attempt_at = now + dt.timedelta(seconds=delay)
            to_status = ConversionJobStatus.FAILED_RETRYABLE
        else:
            job.earliest_next_attempt_at = None
            job.finished_at = now
            to_status = ConversionJobStatus.FAILED_PERM
        job.error_code = error_code
        job.error_message = message
        job.error_detail = error_detail
        job.last_error_at = now
        job.updated_at = now
        record_transition(
            session,
            job,
            to_status=to_status,
            kind=ConversionEventKind.FAILED,
            attempt=job.attempts,
            payload=FailedPayload(
                error_code=error_code,
                error_message=message,
                error_detail=error_detail,
                retryable=retryable,
                last_phase=last_phase,
            ),
        )
        session.commit()
