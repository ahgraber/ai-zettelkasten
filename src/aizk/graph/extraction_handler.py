"""Adapt extraction work-units to :class:`~aizk.pipeline.handler.StageHandler`.

The handler owns extraction-specific claim, recovery, execution, and result
mapping. The runner owns lifecycle transactions, leases, concurrency, timeouts,
and stale recovery. Domain extraction commits its own short transaction. An
in-process cancellation request is honored at execution entry.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
from typing import TYPE_CHECKING

from sqlalchemy import or_
from sqlmodel import Session, select

from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_events import (
    EXTRACTION_STAGE,
    CancelledPayload,
    ClaimedPayload,
    ExtractionEventKind,
    FailedPayload,
    RecoveredStalePayload,
    SucceededPayload,
    TimedOutPayload,
)
from aizk.graph.extraction_run import extract_document
from aizk.pipeline.events import record_transition
from aizk.pipeline.handler import Isolation, StageResult
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.utilities.clock import utcnow

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from aizk.graph.extraction import EntityExtractor
    from aizk.graph.mention_store import InputPolicy

logger = logging.getLogger(__name__)

#: Default wall-clock timeout for one source's extraction unit-of-work.
DEFAULT_TIMEOUT_SECONDS = 600.0
#: Default execution concurrency. One preserves the single serialized SQLite writer.
DEFAULT_CONCURRENCY = 1
#: Default minutes a unit may sit in ``RUNNING`` before stale recovery reclaims it.
DEFAULT_STALE_AFTER_MINUTES = 30.0
#: Default retry backoff base (seconds); the wait is ``base * 2**attempts``.
DEFAULT_RETRY_BASE_DELAY_SECONDS = 2.0
#: Default maximum attempts before a retryable failure becomes permanent.
DEFAULT_RETRY_MAX_ATTEMPTS = 3


def _error_code(exc: BaseException) -> str:
    """Classify an exception into a stable error code."""
    if isinstance(exc, ValueError):
        return "unprocessable_input"
    return type(exc).__name__


def _error_message(exc: BaseException) -> str:
    """Return a scrubbed category message for persistence and the operator API.

    The function excludes raw exception text from those boundaries.
    """
    if isinstance(exc, ValueError):
        return "input could not be processed"
    return "a transient processing error occurred"


class ExtractionStageHandler:
    """Drive extraction work-units through the generic pipeline runner.

    The handler receives the extractor and input policy as dependencies.
    """

    def __init__(
        self,
        engine: "Engine",
        extractor: "EntityExtractor",
        *,
        input_policy: "InputPolicy",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES,
        retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS,
    ) -> None:
        """Configure the extraction stage handler.

        Args:
            engine: Shared engine used by extraction operations.
            extractor: Injected NER extractor.
            input_policy: Input mode for extraction.
            timeout_seconds: Per-unit wall-clock timeout.
            concurrency: Maximum concurrent units.
            stale_after_minutes: Age for stale-unit recovery.
            retry_base_delay_seconds: Base retry delay.
            retry_max_attempts: Maximum attempts before permanent failure.
        """
        self._engine = engine
        self._extractor = extractor
        self._input_policy = input_policy
        self._timeout_seconds = timeout_seconds
        self._concurrency = concurrency
        self._stale_after_minutes = stale_after_minutes
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_attempts = retry_max_attempts
        # Per-handle bridges across the execute/finalize boundary (finalize is
        # handed only (session, handle, outcome)): the produced extraction run
        # id + mention count for the SUCCEEDED event, and the scrubbed error
        # code + message for a FAILED event. Guarded by one lock (N worker
        # threads race). finalize reads them without clearing; cleanup()
        # releases them exactly once, only after the durable terminal
        # transition commits — so a runner finalize-retry (on a finalize-time
        # DB error, slot retained) still sees them.
        self._success: dict[int, tuple[int, int]] = {}
        self._errors: dict[int, tuple[str, str]] = {}
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()

    @property
    def stage(self) -> str:
        """Return the stable stage name used in the correlation spine."""
        return EXTRACTION_STAGE

    def validate_dependencies(self) -> None:
        """Validate dependencies after the composition root constructs the extractor."""
        return None

    def scope_id(self, handle: int) -> str:
        """Return the source ID for a work-unit handle.

        Return the handle as a string when the work-unit row is missing.
        """
        with Session(self._engine) as session:
            job = session.get(ExtractionJob, handle)
            return str(job.source_id) if job is not None else str(handle)

    @property
    def timeout(self) -> datetime.timedelta:
        """Return the wall-clock timeout after which a running unit is terminated."""
        return datetime.timedelta(seconds=self._timeout_seconds)

    @property
    def concurrency_limit(self) -> int:
        """Return the maximum number of units the runner may execute simultaneously."""
        return self._concurrency

    @property
    def isolation(self) -> Isolation:
        """Return the execution isolation: extraction runs in-process."""
        return Isolation.IN_PROCESS

    def map_result(self, result_or_exc: StageResult | BaseException) -> TerminalOutcome:
        """Map execution results and exceptions to terminal outcomes.

        Success maps to ``SUCCEEDED``. ``ValueError`` maps to permanent ``FAILED``;
        other exceptions map to retryable ``FAILED``. The runner handles cancellation
        and timeouts.
        """
        if isinstance(result_or_exc, BaseException):
            if isinstance(result_or_exc, ValueError):
                # Every ValueError the write path raises today is a deterministic
                # provenance/input violation (unchunked source, invalid policy,
                # dangling locator, bad span), so a retry cannot succeed. If a
                # future compaction/retention change makes variant locators
                # transiently dangle, this classification needs revisiting.
                return TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.PERMANENT)
            return TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE)
        return TerminalOutcome(WorkUnitStatus.SUCCEEDED)

    def claim_next(self, session: "Session") -> int | None:
        """Claim the oldest eligible work-unit and transition it to ``RUNNING``.

        Eligible units are queued or retryable failed units whose wait elapsed. The
        method increments attempts, records the claim event, and does not commit.
        Return ``None`` when no unit is eligible.
        """
        now = utcnow()
        job = session.exec(
            select(ExtractionJob)
            .where(
                or_(
                    ExtractionJob.status == WorkUnitStatus.QUEUED,
                    (ExtractionJob.status == WorkUnitStatus.FAILED)
                    & ExtractionJob.earliest_next_attempt_at.is_not(None)  # type: ignore[union-attr]
                    & (ExtractionJob.earliest_next_attempt_at <= now),
                )
            )
            .order_by(ExtractionJob.queued_at)
        ).first()
        if job is None:
            return None

        job.started_at = now
        job.attempts += 1
        job.updated_at = now
        record_transition(
            session,
            job,
            stage=EXTRACTION_STAGE,
            work_unit_ref=str(job.id),
            source_id=job.source_id,
            to_status=WorkUnitStatus.RUNNING,
            kind=ExtractionEventKind.CLAIMED,
            attempt=job.attempts,
            payload=ClaimedPayload(claimed_at=now, worker_pid=os.getpid()),
        )
        return job.id

    def recover_stale(self, session: "Session") -> list[int]:
        """Reset stale ``RUNNING`` units to retryable ``FAILED`` units.

        The method records a recovery event, leaves attempts unchanged, and does not
        commit. Return the reset IDs.
        """
        now = utcnow()
        stale_before = now - datetime.timedelta(minutes=self._stale_after_minutes)
        jobs = session.exec(
            select(ExtractionJob)
            .where(ExtractionJob.status == WorkUnitStatus.RUNNING)
            .where(ExtractionJob.started_at.is_not(None))  # type: ignore[union-attr]
            .where(ExtractionJob.started_at < stale_before)
        ).all()

        recovered: list[int] = []
        for job in jobs:
            last_started_at = job.started_at
            job.earliest_next_attempt_at = now
            job.error_code = "worker_stale_running"
            job.error_message = f"reset after {self._stale_after_minutes} minutes in RUNNING"
            job.last_error_at = now
            job.updated_at = now
            record_transition(
                session,
                job,
                stage=EXTRACTION_STAGE,
                work_unit_ref=str(job.id),
                source_id=job.source_id,
                to_status=WorkUnitStatus.FAILED,
                kind=ExtractionEventKind.RECOVERED_STALE,
                attempt=job.attempts,
                payload=RecoveredStalePayload(
                    stale_after_minutes=self._stale_after_minutes,
                    last_started_at=last_started_at,
                ),
            )
            recovered.append(job.id)  # type: ignore[arg-type]
        return recovered

    def execute(self, handle: int) -> StageResult:
        """Execute one extraction work-unit.

        Skip work already cancelled. Store success or scrubbed failure details for
        finalization, and re-raise execution errors.

        Raises:
            ValueError: If the work-unit or its extraction input is invalid.
        """
        with Session(self._engine) as session:
            job = session.get(ExtractionJob, handle)
            if job is None:
                raise ValueError(f"extraction work-unit {handle} is missing")
            # An operator may have cancelled the unit before execution started; do
            # no work (finalize honors the terminal CANCELLED).
            if job.status is WorkUnitStatus.CANCELLED:
                logger.info("extraction work-unit %s already cancelled; skipping execution", handle)
                return None
            source_id = job.source_id

        # A runner cancel/timeout already requested before work started: skip.
        if self._is_cancelled(handle):
            logger.info("extraction work-unit %s cancelled before start; skipping execution", handle)
            return None

        try:
            result = extract_document(
                self._engine,
                source_id=source_id,
                extractor=self._extractor,
                input_policy=self._input_policy,
            )
        except Exception as exc:
            # Full detail (including the raw message) stays in the internal log; only
            # the stable code + scrubbed category message cross into the audit row/API.
            logger.exception("extraction work-unit %s failed during execution", handle)
            with self._lock:
                self._errors[handle] = (_error_code(exc), _error_message(exc))
            raise

        with self._lock:
            self._success[handle] = (result.run_id, result.mention_count)
        return result

    def finalize(self, session: "Session", handle: int, outcome: TerminalOutcome) -> None:
        """Persist the terminal work-unit status and event in the runner transaction.

        The method does not commit. Failed units retry until the attempt cap; cancelled
        and timed-out units remain terminal. Success events include the extraction run
        ID. Bridge data remains available until :meth:`cleanup` runs after commit.
        """
        with self._lock:
            success = self._success.get(handle)
            error = self._errors.get(handle)

        job = session.get(ExtractionJob, handle)
        if job is None:
            return
        now = utcnow()
        status = outcome.status

        if status is WorkUnitStatus.SUCCEEDED:
            # An operator cancel that landed while the unit ran wins: do not
            # overwrite a terminal CANCELLED with SUCCEEDED.
            if job.status is WorkUnitStatus.CANCELLED:
                logger.info("extraction work-unit %s was cancelled; not overwriting with succeeded", handle)
                return
            job.finished_at = now
            job.updated_at = now
            job.error_code = None
            job.error_message = None
            run_id, mention_count = success if success is not None else (None, 0)
            record_transition(
                session,
                job,
                stage=EXTRACTION_STAGE,
                work_unit_ref=str(job.id),
                source_id=job.source_id,
                to_status=WorkUnitStatus.SUCCEEDED,
                kind=ExtractionEventKind.SUCCEEDED,
                attempt=job.attempts,
                run_id=run_id,
                payload=SucceededPayload(mention_count=mention_count),
            )
            return

        if status is WorkUnitStatus.CANCELLED:
            job.finished_at = now
            job.updated_at = now
            record_transition(
                session,
                job,
                stage=EXTRACTION_STAGE,
                work_unit_ref=str(job.id),
                source_id=job.source_id,
                to_status=WorkUnitStatus.CANCELLED,
                kind=ExtractionEventKind.CANCELLED,
                attempt=job.attempts,
                payload=CancelledPayload(cancellation_reason="runner_cancel"),
            )
            return

        if status is WorkUnitStatus.TIMED_OUT:
            job.finished_at = now
            job.updated_at = now
            record_transition(
                session,
                job,
                stage=EXTRACTION_STAGE,
                work_unit_ref=str(job.id),
                source_id=job.source_id,
                to_status=WorkUnitStatus.TIMED_OUT,
                kind=ExtractionEventKind.TIMED_OUT,
                attempt=job.attempts,
                payload=TimedOutPayload(timeout_seconds=self._timeout_seconds),
            )
            return

        error_code, error_message = error if error is not None else ("extraction_error", "unknown error")
        retryable = outcome.retry_class is RetryClass.RETRYABLE and job.attempts < self._retry_max_attempts
        if retryable:
            delay = self._retry_base_delay_seconds * (2**job.attempts)
            job.earliest_next_attempt_at = now + datetime.timedelta(seconds=delay)
        else:
            job.earliest_next_attempt_at = None
            job.finished_at = now
        job.error_code = error_code
        job.error_message = error_message
        job.last_error_at = now
        job.updated_at = now
        record_transition(
            session,
            job,
            stage=EXTRACTION_STAGE,
            work_unit_ref=str(job.id),
            source_id=job.source_id,
            to_status=WorkUnitStatus.FAILED,
            kind=ExtractionEventKind.FAILED,
            attempt=job.attempts,
            payload=FailedPayload(error_code=error_code, error_message=error_message, retryable=retryable),
        )

    def cleanup(self, handle: int) -> None:
        """Release the unit's per-handle bridge entries; called on every outcome."""
        with self._lock:
            self._success.pop(handle, None)
            self._errors.pop(handle, None)
            self._cancelled.discard(handle)

    def cancel(self, handle: int) -> None:
        """Record a cooperative cancellation request for ``handle``.

        The unit runs in-process and cannot be force-terminated, but the request
        is honored cooperatively: :meth:`execute` checks it at entry and skips the
        unit-of-work entirely for an already-requested cancel. The runner
        resolves the terminal ``CANCELLED`` / ``TIMED_OUT`` status from its own
        slot state.
        """
        with self._lock:
            self._cancelled.add(handle)

    def _is_cancelled(self, handle: int) -> bool:
        """Return whether a handle has an in-process or durable cancellation request.

        The check runs only before extraction starts.
        """
        with self._lock:
            if handle in self._cancelled:
                return True
        with Session(self._engine) as session:
            job = session.get(ExtractionJob, handle)
            return job is not None and job.status is WorkUnitStatus.CANCELLED
