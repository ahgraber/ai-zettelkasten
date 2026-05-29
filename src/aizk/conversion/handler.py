"""Conversion stage's :class:`~aizk.pipeline.handler.StageHandler`.

This adapter exposes the conversion stage's unit-of-work (a conversion job,
keyed by its integer ``ConversionJob.id``) to the generic pipeline runner. The
runner owns the loop, concurrency bound, timeout enforcement, transaction
boundary, and observability; this handler owns the conversion-specific
surface: startup dependency validation, the unit-of-work execution, result
classification, the timeout/concurrency configuration, and the run ``scope_key``.

It implements :meth:`ConversionStageHandler.execute`,
:meth:`ConversionStageHandler.cancel`, and
:meth:`ConversionStageHandler.finalize` by reusing the existing conversion
helpers (``_get_source_ref``, ``_spawn_and_supervise``,
``_write_source_enrichment``, ``_prepare_upload`` / ``_execute_upload``,
``record_transition``, ``classify_job_error``, and
``supervision._terminate_and_wait``). ``finalize`` writes the *failed* /
*timed-out* / *cancelled* terminal statuses into the runner's transaction, while
the *success* terminal status stays fused into ``_execute_upload`` inside
``execute`` (so the SUCCEEDED ``finalize`` branch is an idempotent no-op).

The division — what ``execute`` does vs what the runner / ``finalize`` own:

* ``execute`` runs the conversion unit-of-work middle: preflight source-ref
  fetch, subprocess spawn + supervision, the conversion-private
  ``RUNNING -> UPLOAD_PENDING`` progress transition, best-effort source
  enrichment, and the ``_prepare_upload`` + upload-retry loop (which writes the
  *success* terminal status ``SUCCEEDED`` via ``_execute_upload`` — that fused
  write stays where the conversion pipeline's tests guard it). On a failure it
  RAISES the conversion exception so ``map_result`` classifies it; it does NOT
  write a *failed* terminal status.
* The claim (``RUNNING`` transition + ``attempts`` post-increment + ``claimed``
  event) is owned by ``claim_next``. The job arrives already ``RUNNING`` with
  ``attempts`` incremented, so ``execute`` MUST NOT re-claim it (doing so would
  double-increment ``attempts`` and re-emit ``claimed``).
* The wall-clock timeout is owned by the runner: it calls ``cancel(handle)`` at
  the deadline (the same ``worker_job_timeout_seconds`` value the conversion
  supervision loop uses) and determines ``TIMED_OUT`` / ``CANCELLED`` from its
  own slot state in ``_resolve_outcome`` before ``map_result`` ever runs.
  ``execute`` therefore drives ``_spawn_and_supervise`` with the internal
  deadline disabled and lets ``cancel`` *signal* termination via a per-handle
  event that the supervision loop (the single owner of the subprocess) acts on;
  the cooperative DB-status cancel poll is preserved for API-initiated
  cancellation. The in-process upload phase — which ``cancel`` cannot interrupt
  once the subprocess has exited — is separately re-bound by the same deadline
  inside ``execute`` (raising ``ConversionTimeoutError`` between upload attempts).
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
import tempfile
import threading
import time
from typing import TYPE_CHECKING

from sqlmodel import Session

from aizk.conversion.datamodel.events import (
    ConversionEventKind,
    FailedPayload,
    UploadPendingPayload,
    record_transition,
)
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.paths import metadata_path, read_text_nofollow
from aizk.conversion.workers.errors import (
    ConversionCancelledError,
    ConversionSubprocessError,
    ConversionTimeoutError,
    JobDataIntegrityError,
    ReportedChildError,
    SubprocessMetadataInvalid,
)
from aizk.conversion.workers.orchestrator import (
    JobErrorDetails,
    _get_source_ref,
    _is_job_cancelled,
    _spawn_and_supervise,
    _write_source_enrichment,
    classify_job_error,
)
from aizk.conversion.workers.queries import claim_next_in_session, recover_stale_in_session
from aizk.conversion.workers.types import SubprocessMetadata, _utcnow
from aizk.conversion.workers.uploader import _execute_upload, _prepare_upload
from aizk.pipeline.handler import Isolation, StageResult
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus

if TYPE_CHECKING:
    import multiprocessing as mp

    from aizk.conversion.wiring.worker import WorkerRuntime

logger = logging.getLogger(__name__)


class ConversionStageHandler:
    """Drive conversion jobs through the generic pipeline runner.

    The work-unit handle is the integer ``ConversionJob.id``. The runner passes
    handles through ``execute`` -> ``map_result`` -> ``finalize`` -> ``cleanup``
    without inspecting them; only this handler understands their shape.
    """

    def __init__(self, config: ConversionConfig, runtime: "WorkerRuntime | None" = None) -> None:
        """Store the conversion configuration the runner-facing surface reads.

        Args:
            config: The conversion service configuration, supplying the
                wall-clock timeout (``worker_job_timeout_seconds``) and the
                execution concurrency bound (``worker_concurrency``).
            runtime: The assembled worker runtime (orchestrator, resource guard,
                capabilities). Built lazily on first ``execute`` when omitted so
                construction stays cheap for the trivial runner surface.
        """
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
        # Guards the lazy ``_runtime`` build so the N worker threads racing into
        # the first ``execute`` do not each call ``build_worker_runtime``.
        self._runtime_lock = threading.Lock()

    @property
    def stage(self) -> str:
        """Return the stable stage name used in the correlation spine."""
        return "conversion"

    def validate_dependencies(self) -> None:
        """Validate required external dependencies at startup.

        Conversion has no synchronous startup dependency to probe here: the
        database engine is built lazily per call, and the per-job subprocess
        validates the S3/storage and converter dependencies it needs as part of
        preflight. This is therefore a no-op.
        """
        return None

    def scope_key(self, handle: int) -> str:
        """Return the run ``scope_key`` for ``handle`` (per-job scope).

        Args:
            handle: The conversion job id.

        Returns:
            The job id rendered as a string; conversion scopes runs per job.
        """
        return str(handle)

    @property
    def timeout(self) -> datetime.timedelta:
        """Return the wall-clock timeout after which a running job is terminated."""
        return datetime.timedelta(seconds=self._config.worker_job_timeout_seconds)

    @property
    def concurrency_limit(self) -> int:
        """Return the maximum number of jobs the runner may execute simultaneously."""
        return self._config.worker_concurrency

    @property
    def isolation(self) -> Isolation:
        """Return the execution isolation: conversion runs its work in a subprocess."""
        return Isolation.SUBPROCESS

    def map_result(self, result_or_exc: StageResult | BaseException) -> TerminalOutcome:
        """Map an execution result or exception to a terminal outcome.

        Classification follows the ``retryable`` class attribute on the
        conversion error classes in ``workers/errors.py``:

        * A :class:`~aizk.conversion.workers.errors.ConversionTimeoutError`
          maps to ``TIMED_OUT`` (its own terminal outcome under the generic
          lifecycle).
        * A :class:`~aizk.conversion.workers.errors.ConversionCancelledError`
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

    def claim_next(self, session: "Session") -> int | None:
        """Claim the next eligible job in submission order, or ``None``.

        Runs inside the runner-owned ``BEGIN IMMEDIATE`` transaction: selects
        the oldest eligible job (QUEUED, or FAILED_RETRYABLE past its
        retry-wait), transitions it to RUNNING, **post-increments**
        ``attempts`` (the claim is the attempt counter's source of truth), and
        records the ``claimed`` event. Returns the claimed job id, or ``None``
        when none is eligible. Does NOT commit — the runner owns the transaction
        boundary.

        Delegates to :func:`aizk.conversion.workers.queries.claim_next_in_session`,
        the single source of truth for the claim query + transition.
        """
        return claim_next_in_session(session)

    def recover_stale(self, session: "Session") -> list[int]:
        """Transition jobs stranded in RUNNING back to eligible; return their ids.

        Runs inside the runner-owned ``BEGIN IMMEDIATE`` transaction: reclaims
        RUNNING jobs stranded past the configured stale threshold
        (``worker_stale_job_minutes``) to FAILED_RETRYABLE, recording a
        ``recovered_stale`` event for each. Does NOT commit — the runner owns the
        transaction boundary.

        Delegates to :func:`aizk.conversion.workers.queries.recover_stale_in_session`,
        the single source of truth for the recovery query + transition.
        """
        return recover_stale_in_session(session, self._config)

    def _ensure_runtime(self) -> "WorkerRuntime":
        """Build the worker runtime lazily on first use and cache it.

        Thread-safe (S3): ``execute`` runs on N worker threads, so the lazy
        build is guarded by ``_runtime_lock`` (double-checked) to prevent
        redundant ``build_worker_runtime`` calls and last-writer-wins races. The
        common case (runtime already built) takes no lock.
        """
        if self._runtime is None:
            with self._runtime_lock:
                if self._runtime is None:
                    from aizk.conversion.wiring.worker import build_worker_runtime

                    self._runtime = build_worker_runtime(self._config)
        return self._runtime

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
        2. Snapshot ``(aizk_uuid, attempt)`` once at supervision entry for
           phase / enrichment events that the subprocess cannot see.
        3. Spawn + supervise the conversion subprocess, registering it under the
           handle so ``cancel`` can terminate its process group.
        4. Classify the supervision result, RAISING the matching conversion
           exception on failure so ``map_result`` classifies it. Timeout /
           cancel that the runner itself drove are determined from the runner's
           slot state in ``_resolve_outcome`` and take precedence over whatever
           this raises.
        5. Best-effort source enrichment from the subprocess metadata.
        6. Write the conversion-private ``RUNNING -> UPLOAD_PENDING`` progress
           transition in this method's own short-lived session (``execute`` gets
           no runner session; the runner only ever sees generic ``running``).
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
        runtime = self._ensure_runtime()
        engine = get_engine(config.database_url)

        # The in-process upload phase cannot be interrupted by ``cancel`` (the
        # subprocess is already gone once supervision returns), so re-bind it by
        # the wall-clock deadline the runner enforces. Computed once at execute
        # start from the same ``worker_job_timeout_seconds`` the runner uses,
        # this gates the upload-retry loop (incl. its sleep backoff) so the
        # in-process phase cannot run past the timeout. ``<= 0`` disables it,
        # mirroring the subprocess deadline convention.
        timeout_seconds = config.worker_job_timeout_seconds
        upload_deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None

        # ``claim_next`` has already transitioned the job to RUNNING and
        # post-incremented ``attempts``, so do NOT re-claim here — re-claiming
        # would double-increment ``attempts`` and re-emit ``claimed``.

        source_ref = _get_source_ref(handle, engine)

        converter_name = config.worker_converter_name
        requires_gpu = runtime.capabilities.converter_requires_gpu(converter_name)

        # Snapshot (aizk_uuid, attempt) once at supervision entry.
        # The subprocess cannot see the job's attempt counter, so the parent
        # captures it before phase events start arriving on the queue.
        with Session(engine) as snap_session:
            snap_job = snap_session.get(ConversionJob, handle)
            if snap_job is None:
                raise JobDataIntegrityError(f"Job {handle} missing at supervision entry")
            attempt_snapshot = snap_job.attempts
            aizk_uuid_snapshot = snap_job.aizk_uuid

        def _on_phase_event(phase: str, reported_at: datetime.datetime) -> None:
            """Persist one phase event per subprocess report; best-effort."""
            from aizk.conversion.datamodel.events import record_phase_event

            try:
                with Session(engine) as phase_session:
                    record_phase_event(
                        phase_session,
                        job_id=handle,
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
            # UPLOAD_PENDING transition's payload can carry the content_hash.
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

                    with Session(engine) as session:
                        job_rec = session.get(ConversionJob, handle)
                        if job_rec:
                            _write_source_enrichment(
                                subprocess_meta,
                                str(job_rec.aizk_uuid),
                                engine,
                                job_id=handle,
                                attempt=job_rec.attempts,
                            )
                except Exception:
                    logger.exception("Failed to read SubprocessMetadata for enrichment; job proceeds")

            content_hash = subprocess_meta.markdown_hash_xx64 if subprocess_meta else None
            # Conversion-private RUNNING -> UPLOAD_PENDING progress marker.
            # ``execute`` gets no runner session, so this transition is written
            # through conversion's own short-lived session/transaction. It is not
            # the runner lifecycle (the runner only ever sees generic
            # ``running``); it is a conversion-internal progress entry.
            with Session(engine) as session:
                job_record = session.get(ConversionJob, handle)
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
            # (save_manifest uses O_EXCL); re-running it would deterministically
            # fail attempt 2 with FileExistsError after a transient S3 failure on
            # attempt 1. The retry loop only wraps the S3 PUTs, which are
            # idempotent on their key. A failure here raises so ``map_result``
            # classifies it.
            upload_plan = _prepare_upload(handle, workspace, config)
            if upload_plan is None:
                # Job already finalized (content-hash shortcut, missing, or
                # cancelled): nothing to upload.
                return None

            # Bound the upload phase by the wall-clock deadline before it begins.
            # ``cancel`` can no longer interrupt this in-process phase (the
            # subprocess is gone), so a pre-upload check raises rather than
            # starting an upload already past the deadline.
            if upload_deadline is not None and time.monotonic() >= upload_deadline:
                raise ConversionTimeoutError(f"Job {handle} exceeded its runtime during uploading", "uploading")

            last_exc: Exception | None = None
            for attempt in range(1, config.retry_max_attempts + 1):
                # Re-check the deadline before each attempt so the retry loop
                # (incl. its ``time.sleep`` backoff) cannot run past the timeout.
                if upload_deadline is not None and time.monotonic() >= upload_deadline:
                    raise ConversionTimeoutError(f"Job {handle} exceeded its runtime during uploading", "uploading")
                try:
                    _execute_upload(upload_plan, handle, config)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt == config.retry_max_attempts:
                        break
                    delay = config.retry_base_delay_seconds * (2 ** (attempt - 1))
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

        Wraps :func:`~aizk.conversion.workers.orchestrator._spawn_and_supervise`,
        registering the live subprocess and a per-handle terminate-event under
        ``handle`` via the ``on_spawn`` hook (the seam ``cancel`` reads)
        **before** supervision blocks, and clearing both once supervision returns
        regardless of outcome. The internal supervision deadline is disabled
        (``timeout_seconds=0``) so the runner owns the wall-clock timeout; the
        terminate-event (set by ``cancel``, observed by the supervision loop) is
        the single-owner termination seam — the supervision loop, not ``cancel``,
        joins/terminates the Process.
        """
        engine = get_engine(self._config.database_url)
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

    def finalize(self, session: Session, handle: int, outcome: TerminalOutcome) -> None:
        """Write the job's terminal status into the runner's transaction.

        Writes the *failed* / *timed-out* / *cancelled* terminal statuses. Runs
        inside the runner-owned ``BEGIN IMMEDIATE`` transaction (``session``) and
        **does NOT commit** — the runner commits once. The success terminal write
        (``SUCCEEDED`` + ``ConversionOutput``) is *already* durable from
        ``_execute_upload`` inside :meth:`execute`, so the SUCCEEDED branch is an
        idempotent no-op here.

        Branch on ``outcome.status``:

        * ``SUCCEEDED`` — no-op. ``_execute_upload`` already wrote the success
          terminal status and the output row; ``finalize`` neither re-transitions
          nor emits a second SUCCEEDED event/output. The ``uploaded=False``
          shortcut (``_prepare_upload`` returned ``None`` because the job row was
          missing or already CANCELLED) is also a no-op, leaving the job for the
          next claim / as-cancelled.
        * ``FAILED`` (retryable) and ``TIMED_OUT`` — ``FAILED_RETRYABLE`` with
          ``earliest_next_attempt_at = now + retry_base_delay_seconds * 2**attempts``,
          the ``error_*`` fields, ``last_error_at``, and a ``failed`` event
          carrying ``FailedPayload``. Timeout reuses the retryable path: a
          ``ConversionTimeoutError`` is retryable.
        * ``FAILED`` (permanent) — ``FAILED_PERM`` with ``finished_at = now`` and
          ``earliest_next_attempt_at = None``, the same error fields + event.
        * ``CANCELLED`` — no-op when the job is already ``CANCELLED`` (an
          API-cancelled job is left as-is); when the runner drove a cooperative
          cancel and the DB is not yet ``CANCELLED``, write the ``CANCELLED``
          transition.

        The error fields/scrub come from the per-handle stash ``execute`` recorded
        (the error-details bridge): the egress scrub is already applied there so a
        rejected URL/IP never lands in ``error_message`` / ``error_detail``. The
        stash is read-and-cleared here. ``attempt`` for the event is re-read from
        the row in ``session`` (the claim incremented it), never a stale snapshot.

        Args:
            session: The runner's active ``BEGIN IMMEDIATE`` session.
            handle: The conversion job id.
            outcome: The runner-resolved terminal outcome.
        """
        with self._processes_lock:
            details = self._error_details.pop(handle, None)

        status = outcome.status
        if status is WorkUnitStatus.SUCCEEDED:
            # Success is already terminal (``_execute_upload`` wrote SUCCEEDED +
            # the output). No re-transition, no second event/output. The
            # ``uploaded=False`` shortcut (job missing / cancelled) is likewise a
            # no-op.
            return

        if status is WorkUnitStatus.CANCELLED:
            self._finalize_cancelled(session, handle)
            return

        if status is WorkUnitStatus.TIMED_OUT:
            # A timeout is a retryable ``ConversionTimeoutError`` ->
            # ``FAILED_RETRYABLE``. The bridge stash holds that exception's
            # (scrubbed) details; synthesize a timeout detail if the stash is
            # somehow absent so the row still records the cause.
            details = details or _timeout_details(handle)
            self._finalize_failed(session, handle, details, retryable=True)
            return

        # WorkUnitStatus.FAILED — retry class is required on the outcome.
        details = details or _unknown_failure_details()
        retryable = outcome.retry_class is RetryClass.RETRYABLE
        self._finalize_failed(session, handle, details, retryable=retryable)

    def _finalize_cancelled(self, session: Session, handle: int) -> None:
        """Write the ``CANCELLED`` terminal transition unless already cancelled.

        No-op when the job row is missing or already ``CANCELLED`` (an
        API-cancelled job is left untouched); otherwise records the runner-driven
        cooperative cancellation as a ``cancelled`` event. The event ``attempt``
        is re-read from the row in ``session``. Does not commit.
        """
        from aizk.conversion.datamodel.events import CancelledPayload

        job = session.get(ConversionJob, handle)
        if job is None or job.status == ConversionJobStatus.CANCELLED:
            return
        now = _utcnow()
        job.updated_at = now
        job.finished_at = now
        record_transition(
            session,
            job,
            to_status=ConversionJobStatus.CANCELLED,
            kind=ConversionEventKind.CANCELLED,
            attempt=job.attempts,
            payload=CancelledPayload(cancellation_reason="runner_cancel"),
        )

    def _finalize_failed(
        self,
        session: Session,
        handle: int,
        details: JobErrorDetails,
        *,
        retryable: bool,
    ) -> None:
        """Write a ``FAILED_RETRYABLE`` / ``FAILED_PERM`` transition + event.

        The retryable branch sets ``earliest_next_attempt_at = now +
        retry_base_delay_seconds * 2**attempts``; the permanent branch sets
        ``finished_at`` and clears ``earliest_next_attempt_at``. Both write the
        (already-scrubbed) ``error_*`` fields, ``last_error_at``, and a
        ``failed`` event carrying ``FailedPayload``. No-op when the job row is
        missing or already ``CANCELLED``. The event ``attempt`` is re-read from
        the row in ``session``. Does not commit.
        """
        job = session.get(ConversionJob, handle)
        if job is None or job.status == ConversionJobStatus.CANCELLED:
            return
        now = _utcnow()
        if retryable:
            delay = self._config.retry_base_delay_seconds * (2**job.attempts)
            job.earliest_next_attempt_at = now + datetime.timedelta(seconds=delay)
            to_status = ConversionJobStatus.FAILED_RETRYABLE
        else:
            job.earliest_next_attempt_at = None
            job.finished_at = now
            to_status = ConversionJobStatus.FAILED_PERM
        job.error_code = details.error_code
        job.error_message = details.error_message
        job.error_detail = details.error_detail
        job.last_error_at = now
        job.updated_at = now
        record_transition(
            session,
            job,
            to_status=to_status,
            kind=ConversionEventKind.FAILED,
            attempt=job.attempts,
            payload=FailedPayload(
                error_code=details.error_code,
                error_message=details.error_message,
                error_detail=details.error_detail,
                retryable=retryable,
                last_phase=details.last_phase,
            ),
        )

    def cleanup(self, handle: int) -> None:
        """Release the job's transient per-handle resources.

        Drops any tracked subprocess reference, terminate-event, and stashed
        error-details for ``handle``. The subprocess itself is reaped inside
        ``execute`` (the supervision loop joins it on normal completion or after
        ``cancel`` signals termination); this only clears the runner-visible
        tracking entries so a finished handle leaves no dangling reference.
        Clearing the error-details stash here (in addition to ``finalize``'s
        read-and-clear) guarantees the bridge dict cannot grow unbounded even on
        a terminal outcome that never finalizes the failed path. Idempotent and
        never raises — the runner calls it on every terminal outcome.
        """
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


def _timeout_details(handle: int) -> JobErrorDetails:
    """Synthesize the failure details for a runner-driven timeout.

    The error-details bridge normally supplies the stashed
    ``ConversionTimeoutError`` details for a ``TIMED_OUT`` outcome. This is the
    fallback for the (unexpected) case where the stash is absent — e.g. the
    runner resolved ``TIMED_OUT`` from its slot state while ``execute`` returned
    without raising. It mirrors the ``ConversionTimeoutError`` contract
    (``error_code='conversion_timeout'``, retryable) so the persisted row still
    records the cause.
    """
    return classify_job_error(ConversionTimeoutError(f"Job {handle} timed out", "unknown"))


def _unknown_failure_details() -> JobErrorDetails:
    """Synthesize generic retryable failure details when none were stashed.

    Defensive fallback for a ``FAILED`` outcome with no stashed details (which
    should not happen — ``execute``'s wrapper stashes on every ``Exception``):
    code ``conversion_failed``, retryable.
    """
    return JobErrorDetails(
        error_code="conversion_failed",
        error_message="conversion_failed",
        error_detail=None,
        retryable=True,
        last_phase=None,
    )
