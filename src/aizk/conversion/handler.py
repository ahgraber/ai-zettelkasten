"""Conversion stage's :class:`~aizk.pipeline.handler.StageHandler`.

This adapter exposes the conversion stage's unit-of-work (a conversion job,
keyed by its integer ``ConversionJob.id``) to the generic pipeline runner. The
runner owns the loop, concurrency bound, timeout enforcement, transaction
boundary, and observability; the lifecycle (claim, stale recovery, terminal
transition, cancel) is :class:`~aizk.pipeline.stage.HandlerBase` over the
work-unit repository. This handler owns the conversion-specific surface:
startup dependency validation, subprocess isolation, the unit-of-work
execution, result classification, the job ``phase`` column, and the run
``scope_id``.

It implements :meth:`ConversionStageHandler.execute` and
:meth:`ConversionStageHandler.cancel` by reusing the existing conversion
helpers (``_get_source_ref``, ``_spawn_and_supervise``,
``_write_source_enrichment``, ``_prepare_upload`` / ``_execute_upload``,
``classify_job_error``, and ``supervision._terminate_and_wait``). The
repository writes the *failed* / *timed-out* / *cancelled* terminal statuses
into the runner's transaction, while the *success* terminal status stays fused
into ``_execute_upload`` inside ``execute`` (so the SUCCEEDED ``finalize``
branch only reports whether that write landed).

The division — what ``execute`` does vs what the runner / ``finalize`` own:

* ``execute`` runs the conversion unit-of-work middle: preflight source-ref
  fetch, subprocess spawn + supervision, best-effort source enrichment, the
  ``phase = upload`` write with the ``conversion_completed`` event, and the
  ``_prepare_upload`` + upload-retry loop (which writes the *success* terminal
  status ``SUCCEEDED`` via ``_execute_upload`` — that fused write stays where
  the conversion pipeline's tests guard it). On a failure it RAISES the
  conversion exception so ``map_result`` classifies it; it does NOT write a
  *failed* terminal status.
* The claim (``RUNNING`` transition + ``attempts`` post-increment + ``claimed``
  event + ``phase = convert``) is owned by ``claim_next``. The job arrives
  already ``RUNNING`` with ``attempts`` incremented, so ``execute`` MUST NOT
  re-claim it (doing so would double-increment ``attempts`` and re-emit
  ``claimed``).
* The wall-clock timeout is owned by the runner: it calls ``cancel(handle)`` at
  the deadline (the same ``worker_job_timeout_seconds`` value the conversion
  supervision loop uses) and determines ``TIMED_OUT`` / ``CANCELLED`` from its
  own slot state in ``_resolve_outcome`` before ``map_result`` ever runs.
  ``execute`` therefore drives ``_spawn_and_supervise`` with the internal
  deadline disabled and lets ``cancel`` *signal* termination via a per-handle
  event that the supervision loop (the single owner of the subprocess) acts on;
  the cooperative DB-status cancel poll is preserved for API-initiated
  cancellation. The in-process upload phase cannot be interrupted once the
  subprocess has exited; a timeout during upload is recorded when the upload
  returns, and not over a job the upload already settled.
"""

from __future__ import annotations

from dataclasses import replace
import datetime
import logging
from pathlib import Path
import tempfile
import threading
import time
from typing import TYPE_CHECKING

from sqlmodel import Session

from aizk.conversion.datamodel.events import FailedPayload, record_conversion_completed
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.processing.errors import (
    ConversionCancelledError,
    ConversionRuntimeMissingError,
    ConversionSubprocessError,
    ConversionTimeoutError,
    JobDataIntegrityError,
    JobErrorDetails,
    ReportedChildError,
    SubprocessMetadataInvalid,
    classify_job_error,
)
from aizk.conversion.processing.source import _get_source_ref, _write_source_enrichment
from aizk.conversion.processing.subproc import _is_job_cancelled, _spawn_and_supervise
from aizk.conversion.processing.types import SubprocessMetadata
from aizk.conversion.processing.uploader import _execute_upload, _prepare_upload
from aizk.conversion.settings import ConversionConfig
from aizk.conversion.stage import CONVERSION_DEFINITION, CONVERT_PHASE, UPLOAD_PHASE, cancellable
from aizk.conversion.storage.paths import metadata_path, read_text_nofollow
from aizk.db.config import DatabaseConfig
from aizk.db.engine import begin_immediate_on, get_engine
from aizk.pipeline.handler import Isolation, StageResult
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.pipeline.stage import HandlerBase
from aizk.utilities.clock import utcnow
from aizk.utilities.errors import StartupValidationError

if TYPE_CHECKING:
    import multiprocessing as mp

    from aizk.conversion.wiring.worker import WorkerRuntime

logger = logging.getLogger(__name__)


class ConversionStageHandler(HandlerBase[ConversionJob]):
    """Drive conversion jobs through the generic pipeline runner.

    The work-unit handle is the integer ``ConversionJob.id``. The runner passes
    handles through ``execute`` -> ``map_result`` -> ``finalize`` -> ``cleanup``
    without inspecting them; only this handler understands their shape.
    """

    default_error = ("conversion_failed", "conversion_failed")

    def __init__(self, config: ConversionConfig, runtime: "WorkerRuntime | None" = None) -> None:
        """Store the conversion configuration the runner-facing surface reads.

        Args:
            config: The conversion service configuration, supplying the
                wall-clock timeout (``worker_job_timeout_seconds``) and the
                execution concurrency bound (``worker_concurrency``).
            runtime: The assembled worker runtime (coordinator, resource guard,
                capabilities).
        """
        super().__init__(
            get_engine(DatabaseConfig().database_url),
            replace(CONVERSION_DEFINITION, failed_payload=self._failed_payload),
            timeout_seconds=config.worker_job_timeout_seconds,
            concurrency=config.worker_concurrency,
            stale_after_minutes=config.worker_stale_job_minutes,
            retry_base_delay_seconds=config.retry_base_delay_seconds,
            retry_max_attempts=config.worker_max_attempts,
        )
        self._config = config
        self._runtime = runtime
        # Per-handle subprocess tracking, the seam ``cancel`` uses to *signal*
        # termination of a running job's subprocess. ``execute`` registers the
        # spawned process and a per-handle terminate-event under its handle
        # before supervision blocks, and clears both once supervision returns.
        # ``cancel`` reads the event under the lock and sets it; it never joins
        # the Process. The supervision loop (single owner of the Process)
        # observes the event each poll iteration and performs the actual
        # graceful-before-forceful terminate/join itself, so the runner driver
        # thread and the worker thread never join the same Process concurrently.
        self._processes: dict[int, "mp.Process"] = {}
        self._terminate_events: dict[int, threading.Event] = {}
        # The error-details bridge: ``finalize`` is handed only
        # ``(session, handle, outcome)`` — never the exception ``execute``
        # raised — yet the FAILED path must persist ``error_code`` /
        # ``error_message`` / ``error_detail`` (and the egress scrub needs the
        # code). ``execute`` stashes the *scrubbed* :class:`JobErrorDetails`
        # under the handle on its failure path; ``finalize`` reads + clears it,
        # and ``cleanup`` clears it too so a finalize-less terminal outcome
        # cannot leak an entry and grow the dict unbounded. Guarded by the same
        # ``_processes_lock`` as the subprocess tracking (N worker threads
        # race here).
        self._error_details: dict[int, JobErrorDetails] = {}
        self._processes_lock = threading.Lock()

    def validate_dependencies(self) -> None:
        """Run the adapter-declared startup probes, gating work acceptance.

        The probe set is assembled from the registered adapters and
        configuration (S3 and the database always; KaraKeep when its resolver is
        registered; the picture-description endpoint when configured). A probe
        that raises ``StartupValidationError`` propagates so the runner refuses
        to accept work.
        """
        if self._runtime is None:
            raise StartupValidationError("conversion runtime was not injected")
        for probe in self._runtime.capabilities.startup_probes:
            probe()

    def scope_id(self, handle: int) -> str:
        """Return the run ``scope_id`` for ``handle`` (per-job scope).

        Args:
            handle: The conversion job id.

        Returns:
            The job id rendered as a string; conversion scopes runs per job.
        """
        return str(handle)

    @property
    def isolation(self) -> Isolation:
        """Return the execution isolation: conversion runs its work in a subprocess."""
        return Isolation.SUBPROCESS

    def map_result(self, result_or_exc: StageResult | BaseException) -> TerminalOutcome:
        """Map an execution result or exception to a terminal outcome.

        Classification follows the ``retryable`` class attribute on the
        conversion error classes in ``processing/errors.py``:

        * A :class:`~aizk.conversion.processing.errors.ConversionTimeoutError`
          maps to ``TIMED_OUT`` (its own terminal outcome under the generic
          lifecycle).
        * A :class:`~aizk.conversion.processing.errors.ConversionCancelledError`
          maps to ``CANCELLED``.
        * Any other exception maps to ``FAILED``, with the retry class taken
          from the exception's ``retryable`` attribute, defaulting to retryable
          when the attribute is absent.
        * A successful (non-exception) result maps to ``SUCCEEDED``.

        Args:
            result_or_exc: The stage result from a successful ``execute``, or the
                exception it raised.

        Returns:
            The terminal outcome bundling the lifecycle status with the retry
            classification (the latter only for a ``FAILED`` outcome).
        """
        if isinstance(result_or_exc, ConversionTimeoutError):
            return TerminalOutcome(WorkUnitStatus.TIMED_OUT)
        if isinstance(result_or_exc, ConversionCancelledError):
            return TerminalOutcome(WorkUnitStatus.CANCELLED)
        if isinstance(result_or_exc, BaseException):
            retryable = bool(getattr(result_or_exc, "retryable", True))
            retry_class = RetryClass.RETRYABLE if retryable else RetryClass.PERMANENT
            return TerminalOutcome(WorkUnitStatus.FAILED, retry_class)
        return TerminalOutcome(WorkUnitStatus.SUCCEEDED)

    def cancellable(self, row: ConversionJob) -> bool:
        """Report whether a ``running`` job may be cancelled: not in its upload phase."""
        return cancellable(row)

    def on_claim(self, session: Session, row: ConversionJob) -> None:
        """Set the claimed job's phase to ``convert``."""
        row.phase = CONVERT_PHASE

    def on_transition(self, session: Session, row: ConversionJob, to_status: WorkUnitStatus) -> None:
        """Clear the phase on leaving ``running``; record a timeout's or failure's error fields."""
        if to_status is WorkUnitStatus.TIMED_OUT:
            row.error_code = ConversionTimeoutError.error_code
            row.error_message = f"Job {row.id} exceeded its runtime during {row.phase}"
            row.last_error_at = row.finished_at
        elif to_status is WorkUnitStatus.FAILED:
            details = self._matching_details(row.id, row.error_code)
            row.error_detail = details.error_detail if details is not None else None
        row.phase = None

    def _matching_details(self, job_id: int | None, error_code: str | None) -> JobErrorDetails | None:
        with self._processes_lock:
            details = self._error_details.get(job_id) if job_id is not None else None
        return details if details is not None and details.error_code == error_code else None

    def _failed_payload(
        self, row: ConversionJob, *, error_code: str, error_message: str, retryable: bool
    ) -> FailedPayload:
        details = self._matching_details(row.id, error_code)
        return FailedPayload(
            error_code=error_code,
            error_message=error_message,
            error_detail=details.error_detail if details is not None else None,
            retryable=retryable,
            last_phase=details.last_phase if details is not None else None,
        )

    def execute(self, handle: int) -> StageResult:
        """Run the conversion unit-of-work for ``handle``, bridging failure details.

        Wraps :meth:`_execute` with the error-details bridge: on any
        :class:`Exception`, stashes the scrubbed :class:`JobErrorDetails` under
        ``handle`` (via ``classify_job_error``) so ``finalize`` — which receives
        only ``(session, handle, outcome)`` — can persist the error fields, then
        re-raises so ``map_result`` classifies it. ``BaseException`` subclasses
        that are not ``Exception`` (e.g. ``KeyboardInterrupt``) are not stashed.
        """
        try:
            return self._execute(handle)
        except Exception as exc:
            with self._processes_lock:
                self._error_details[handle] = classify_job_error(exc)
            raise

    def _execute(self, handle: int) -> StageResult:  # noqa: C901
        """Run the conversion unit-of-work for ``handle``; raise on failure.

        The claim and the failure-terminal writes belong to ``claim_next`` and
        to ``finalize`` / ``map_result`` respectively, so they are not done here.
        The job arrives already ``RUNNING`` with ``attempts`` post-incremented by
        ``claim_next``; this method MUST NOT re-claim.

        Steps (all reusing existing helpers):

        1. Preflight: fetch + validate the job's ``source_ref``.
        2. Snapshot ``(source_id, attempt)`` once at supervision entry for
           phase / enrichment events that the subprocess cannot see.
        3. Spawn + supervise the conversion subprocess, registering it under the
           handle so ``cancel`` can terminate its process group.
        4. Classify the supervision result, RAISING the matching conversion
           exception on failure so ``map_result`` classifies it. Timeout /
           cancel that the runner itself drove are determined from the runner's
           slot state in ``_resolve_outcome`` and take precedence over whatever
           this raises.
        5. Best-effort source enrichment from the subprocess metadata.
        6. Write ``phase = upload`` with the ``conversion_completed`` event in
           this method's own ``BEGIN IMMEDIATE`` transaction, refusing a job
           that is no longer ``running``.
        7. ``_prepare_upload`` once, then the S3-PUT retry loop via
           ``_execute_upload`` (which writes the ``SUCCEEDED`` terminal status —
           that fused success write stays in the conversion uploader where its
           tests guard it).

        Returns:
            ``None`` on success. The success terminal status (``SUCCEEDED`` +
            ``ConversionOutput``) is already durable from ``_execute_upload``;
            returning normally (not raising) is the success signal ``map_result``
            maps to ``SUCCEEDED``.

        Raises:
            JobDataIntegrityError: If the job row or its ``source_ref`` is missing.
            ConversionTimeoutError: If the subprocess exceeded its runtime.
            ConversionCancelledError: If the job was cancelled mid-execution.
            ConversionSubprocessError: If the subprocess exited non-zero without
                a structured error report.
            ReportedChildError: If the subprocess reported a structured failure.
            Exception: Any error raised by ``_prepare_upload`` /
                ``_execute_upload`` (e.g. ``ConversionArtifactsMissingError``,
                ``SubprocessMetadataInvalid``, ``S3Error``).
        """
        config = self._config
        runtime = self._runtime
        if runtime is None:
            raise ConversionRuntimeMissingError("conversion runtime was not injected")
        engine = get_engine(DatabaseConfig().database_url)
        with self._lock:
            claimed_attempt = self._attempts.get(handle)

        # ``claim_next`` has already transitioned the job to RUNNING and
        # post-incremented ``attempts``, so do NOT re-claim here — re-claiming
        # would double-increment ``attempts`` and re-emit ``claimed``.

        source_ref = _get_source_ref(handle, engine)

        converter_name = config.worker_converter_name
        requires_gpu = runtime.capabilities.converter_requires_gpu(converter_name)

        # Snapshot (source_id, attempt) once at supervision entry.
        # The subprocess cannot see the job's attempt counter, so the parent
        # captures it before phase events start arriving on the queue.
        with Session(engine) as snap_session:
            snap_job = snap_session.get(ConversionJob, handle)
            if snap_job is None:
                raise JobDataIntegrityError(f"Job {handle} missing at supervision entry")
            attempt_snapshot = snap_job.attempts if claimed_attempt is None else claimed_attempt
            source_id_snapshot = snap_job.source_id
            started_at = snap_job.started_at or self._clock()
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=datetime.timezone.utc)
        deadline = started_at + datetime.timedelta(seconds=config.worker_job_timeout_seconds)

        def _on_phase_event(phase: str, reported_at: datetime.datetime) -> None:
            """Persist one phase event per subprocess report; best-effort."""
            # Imported here: tests patch aizk.conversion.datamodel.events.record_phase_event at its definition.
            from aizk.conversion.datamodel.events import record_phase_event

            try:
                with Session(engine) as phase_session:
                    begin_immediate_on(phase_session)
                    if not _holds_claim(phase_session, handle, attempt_snapshot):
                        logger.warning(
                            "Phase event skipped: job %s is not running on attempt %d", handle, attempt_snapshot
                        )
                        return
                    record_phase_event(
                        phase_session,
                        job_id=handle,
                        source_id=source_id_snapshot,
                        attempt=attempt_snapshot,
                        current_status=WorkUnitStatus.RUNNING,
                        phase=phase,
                        reported_at=reported_at,
                    )
                    phase_session.commit()
            except Exception:
                logger.warning(
                    "Failed to commit phase event for job %s phase %r (best-effort)",
                    handle,
                    phase,
                    exc_info=True,
                )

        with tempfile.TemporaryDirectory() as tmpdirname:
            workspace = Path(tmpdirname)
            source_ref_json = source_ref.model_dump_json()

            # The runner owns the wall-clock timeout: it calls ``cancel(handle)``
            # at the deadline, which *signals* the per-handle terminate-event that
            # the supervision loop (the single owner of the subprocess) acts on,
            # and marks its slot ``timed_out`` so ``_resolve_outcome`` returns
            # ``TIMED_OUT`` regardless of what this method raises. So the internal
            # supervision deadline is disabled here (``timeout_seconds=0`` ->
            # ``deadline=None``). The DB-status cancel poll is preserved for
            # API-initiated cancellation. The runner uses the same configured
            # value (``worker_job_timeout_seconds``) via ``self.timeout``.
            process, result, _deadline = self._spawn_and_track(
                handle=handle,
                workspace=workspace,
                source_ref_json=source_ref_json,
                runtime=runtime,
                requires_gpu=requires_gpu,
                on_phase_event=_on_phase_event,
            )

            if result.timed_out:
                raise ConversionTimeoutError(
                    f"Job {handle} exceeded its runtime during {result.last_phase}",
                    result.last_phase,
                )

            if result.shutdown_terminated:
                raise ConversionTimeoutError(
                    f"Job {handle} terminated during shutdown drain in {result.last_phase}",
                    result.last_phase,
                )

            if result.cancelled:
                # The terminal status is owned by the lifecycle: raise so
                # ``map_result`` records the CANCELLED outcome.
                raise ConversionCancelledError(f"Job {handle} cancelled during {result.last_phase}")

            if result.reported_error:
                error_code = result.reported_error.get("error_code", "conversion_failed")
                error_message = result.reported_error.get("message", "conversion_failed")
                retryable: bool | None = None
                retryable_value = result.reported_error.get("retryable")
                if retryable_value is not None:
                    retryable = str(retryable_value).lower() == "true"
                raise ReportedChildError(
                    error_message,
                    error_code,
                    retryable=retryable,
                    traceback=result.reported_error.get("traceback"),
                )

            if process.exitcode and process.exitcode != 0:
                raise ConversionSubprocessError(f"Job {handle} subprocess exited with code {process.exitcode}")

            if _is_job_cancelled(handle, engine):
                raise ConversionCancelledError(f"Job {handle} cancelled before upload")

            # Best-effort Source enrichment from subprocess-written metadata.
            # Uses typed SubprocessMetadata so schema drift fails loudly rather
            # than silently dropping fields. Failure here does not affect manifest
            # or output. We also pull subprocess_meta into outer scope so the
            # conversion_completed event's payload can carry the content_hash.
            subprocess_meta: SubprocessMetadata | None = None
            metadata_file = metadata_path(workspace)
            if metadata_file.exists():
                try:
                    from pydantic import ValidationError

                    raw = read_text_nofollow(metadata_file)
                    try:
                        subprocess_meta = SubprocessMetadata.model_validate_json(raw)
                    except ValidationError as exc:
                        raise SubprocessMetadataInvalid(
                            f"metadata.json failed SubprocessMetadata validation: {exc}"
                        ) from exc

                    _write_source_enrichment(
                        subprocess_meta,
                        str(source_id_snapshot),
                        engine,
                        job_id=handle,
                        attempt=attempt_snapshot,
                        holds_claim=lambda session: _holds_claim(session, handle, attempt_snapshot),
                    )
                except Exception:
                    logger.exception("Failed to read SubprocessMetadata for enrichment; job proceeds")

            content_hash = subprocess_meta.markdown_hash_xx64 if subprocess_meta else None
            _start_upload_phase(engine, handle, content_hash, attempt_snapshot)

            # Local prep (read metadata, generate + write manifest) runs exactly
            # once outside the retry loop. It is not idempotent against itself
            # (save_manifest uses O_EXCL); re-running it would deterministically
            # fail attempt 2 with FileExistsError after a transient S3 failure on
            # attempt 1. The retry loop only wraps the S3 PUTs, which are
            # idempotent on their key. A failure here raises so ``map_result``
            # classifies it.
            upload_plan = _prepare_upload(handle, workspace, runtime.object_storage, attempt=attempt_snapshot)
            if upload_plan is None:
                raise ConversionCancelledError(f"Job {handle} cancelled or missing before upload")

            last_exc: Exception | None = None
            for attempt in range(1, config.retry_max_attempts + 1):
                _raise_past_deadline(self._clock(), deadline, handle)
                try:
                    _execute_upload(upload_plan, handle, runtime.object_storage)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt == config.retry_max_attempts:
                        break
                    now = self._clock()
                    _raise_past_deadline(now, deadline, handle)
                    delay = min(
                        config.retry_base_delay_seconds * (2 ** (attempt - 1)), (deadline - now).total_seconds()
                    )
                    logger.warning(
                        "Upload attempt %d failed for job %s; retrying in %s seconds: %s",
                        attempt,
                        handle,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
            if last_exc is not None:
                # Exhausted retries: raise the last upload error so ``map_result``
                # classifies it.
                raise last_exc

            return None

    def _spawn_and_track(
        self,
        *,
        handle: int,
        workspace: Path,
        source_ref_json: str,
        runtime: "WorkerRuntime",
        requires_gpu: bool,
        on_phase_event,
    ):
        """Spawn + supervise the subprocess, registering it for ``cancel``.

        Wraps :func:`~aizk.conversion.processing.subproc._spawn_and_supervise`,
        registering the live subprocess and a per-handle terminate-event under
        ``handle`` via the ``on_spawn`` hook (the seam ``cancel`` reads)
        **before** supervision blocks, and clearing both once supervision returns
        regardless of outcome. The internal supervision deadline is disabled
        (``timeout_seconds=0``) so the runner owns the wall-clock timeout; the
        terminate-event (set by ``cancel``, observed by the supervision loop) is
        the single-owner termination seam — the supervision loop, not ``cancel``,
        joins/terminates the Process.
        """
        engine = get_engine(DatabaseConfig().database_url)
        terminate_event = threading.Event()

        def _register(process: "mp.Process") -> None:
            with self._processes_lock:
                self._processes[handle] = process
                self._terminate_events[handle] = terminate_event

        try:
            process, result, deadline = _spawn_and_supervise(
                job_id=handle,
                workspace=workspace,
                source_ref_json=source_ref_json,
                poll_interval_seconds=2.0,
                timeout_seconds=0.0,
                is_cancelled_fn=lambda: _is_job_cancelled(handle, engine),
                config=self._config,
                resource_guard=runtime.resource_guard,
                requires_gpu=requires_gpu,
                spawn_subprocess=runtime.spawn_subprocess,
                on_phase_event=on_phase_event,
                on_spawn=_register,
                terminate_event=terminate_event,
            )
        finally:
            # Supervision has returned: the process is no longer alive (joined or
            # terminated by the supervision loop). Drop the tracking entries so a
            # later ``cancel`` for this handle is a no-op rather than signalling a
            # dead pid.
            with self._processes_lock:
                self._processes.pop(handle, None)
                self._terminate_events.pop(handle, None)
        return process, result, deadline

    def finalize(self, session: Session, handle: int, outcome: TerminalOutcome) -> bool:
        """Write the job's terminal status into the runner's transaction.

        Runs inside the runner-owned ``BEGIN IMMEDIATE`` transaction
        (``session``) and **does NOT commit** — the runner commits once. The
        success terminal write (``SUCCEEDED`` + ``ConversionOutput``) is
        *already* durable from ``_execute_upload`` inside :meth:`execute`, so a
        ``SUCCEEDED`` outcome writes nothing here and reports whether the job
        reads ``succeeded`` at the finalizing attempt. Every other outcome goes
        through the repository, which writes only while the job is still
        ``running`` on that attempt.

        The error fields come from the per-handle stash ``execute`` recorded
        (the error-details bridge): the egress scrub is already applied there so
        a rejected URL/IP never lands in ``error_message`` / ``error_detail``.
        The stash is **read but not cleared** here: the runner retains the slot
        and re-invokes ``finalize`` on a finalize-time DB error, so clearing now
        would lose the real scrubbed cause. :meth:`cleanup` releases it, once,
        only after the durable commit (it runs on every terminal outcome).

        Args:
            session: The runner's active ``BEGIN IMMEDIATE`` session.
            handle: The conversion job id.
            outcome: The runner-resolved terminal outcome.

        Returns:
            Whether the job's terminal status is this attempt's outcome.
        """
        if outcome.status is not WorkUnitStatus.SUCCEEDED:
            with self._processes_lock:
                details = self._error_details.get(handle)
            if details is not None:
                with self._lock:
                    self._errors[handle] = (details.error_code, details.error_message)
            return super().finalize(session, handle, outcome)
        with self._lock:
            attempt = self._attempts.get(handle)
        job = session.get(ConversionJob, handle)
        if job is None or WorkUnitStatus(job.status) is not WorkUnitStatus.SUCCEEDED:
            return False
        return attempt is None or job.attempts == attempt

    def cleanup(self, handle: int) -> None:
        """Release the job's transient per-handle resources.

        Drops any tracked subprocess reference, terminate-event, and stashed
        error-details for ``handle``. The subprocess itself is reaped inside
        ``execute`` (the supervision loop joins it on normal completion or after
        ``cancel`` signals termination); this only clears the runner-visible
        tracking entries so a finished handle leaves no dangling reference. This is
        the **sole** clear point for the error-details bridge (``finalize`` only
        reads it): the runner calls ``cleanup`` exactly once per handle, only after
        the durable terminal transition commits, so a finalize-time DB-error retry
        still sees the stashed cause while the bridge can never grow unbounded.
        Idempotent and never raises — the runner calls it on every terminal outcome.
        """
        super().cleanup(handle)
        with self._processes_lock:
            self._processes.pop(handle, None)
            self._terminate_events.pop(handle, None)
            self._error_details.pop(handle, None)

    def cancel(self, handle: int) -> None:
        """Signal termination of the running subprocess for ``handle``.

        Single-owner termination: this method only **signals** — it sets
        the per-handle terminate-event ``execute`` registered alongside the
        tracked process and returns promptly. It does NOT join or terminate the
        Process. The supervision loop running on the worker thread (the single
        owner of the ``mp.Process``) observes the event each poll iteration and
        performs the graceful-before-forceful process-group termination + join
        itself, so the runner driver thread and the worker thread never join
        the same Process concurrently. A no-op when nothing is running for
        ``handle``.

        This is the single path the runner uses for both wall-clock timeout and
        cooperative cancellation (and drain-timeout survivor termination).
        Termination latency is bounded by the supervision poll interval.
        """
        with self._processes_lock:
            terminate_event = self._terminate_events.get(handle)
        if terminate_event is None:
            return
        terminate_event.set()


def _holds_claim(session: Session, handle: int, attempt: int) -> bool:
    job = session.get(ConversionJob, handle)
    return job is not None and WorkUnitStatus(job.status) is WorkUnitStatus.RUNNING and job.attempts == attempt


def _raise_past_deadline(now: datetime.datetime, deadline: datetime.datetime, handle: int) -> None:
    if now >= deadline:
        raise ConversionTimeoutError(f"Job {handle} exceeded its runtime during {UPLOAD_PHASE}", UPLOAD_PHASE)


def _start_upload_phase(engine, handle: int, content_hash: str | None, attempt: int) -> None:
    with Session(engine) as session:
        begin_immediate_on(session)
        job = session.get(ConversionJob, handle)
        if job is None or WorkUnitStatus(job.status) is not WorkUnitStatus.RUNNING:
            session.rollback()
            raise ConversionCancelledError(f"Job {handle} is no longer running before upload")
        if job.attempts != attempt:
            session.rollback()
            raise ConversionCancelledError(
                f"Job {handle} is on attempt {job.attempts}, not attempt {attempt}, before upload"
            )
        job.phase = UPLOAD_PHASE
        job.updated_at = utcnow()
        session.add(job)
        record_conversion_completed(session, job, content_hash=content_hash)
        session.commit()
