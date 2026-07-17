"""The extraction stage's :class:`~aizk.pipeline.handler.StageHandler`.

This adapter exposes the extraction stage's per-source unit-of-work (extract
and persist one source's entity mentions and co-occurrence links, keyed by its
integer ``ExtractionJob.id``) to the generic pipeline runner. The runner owns
the claim/lease loop, bounded concurrency, wall-clock timeout, stale-recovery
scheduling, and the ``BEGIN IMMEDIATE`` transaction around ``claim_next`` /
``recover_stale`` / ``finalize``; this handler owns the extraction-specific
surface: the claim/recovery queries, the unit-of-work execution, result
classification, the run ``scope_id`` (the source ``source_id``), and the
in-process timeout/concurrency declarations.

Transaction ownership and at-least-once (per the change's design):

- ``claim_next`` / ``recover_stale`` / ``finalize`` run inside the runner-owned
  transaction and never commit — their work-unit status transitions co-commit
  with the runner's bookkeeping.
- ``execute`` runs **outside** that transaction and calls
  :func:`aizk.graph.extraction_run.extract_document`, which opens its own
  short ``BEGIN IMMEDIATE`` to commit the extraction run and its mention /
  co-occurrence rows in one transaction. The work-unit's terminal transition
  therefore commits separately from the domain writes; if the process dies
  between them the unit is recovered (``recover_stale``) and ``execute``
  re-runs. Re-execution is safe because ``extract_document`` is idempotent on
  its derivation key — an unchanged source reuses its active extraction run
  and produces no duplicate mention or co-occurrence rows.

The work-unit's transition events carry the ``source_id`` source identity and,
on the terminal success event, the extraction run's ``run_id``, so a source's
progress is resolvable across stages.

Cancellation is cooperative and best-effort, mirroring the contextualization
handler, with one narrowing: ``extract_document`` (unlike contextualization's
``process_document``) takes no ``is_cancelled`` callback, since a per-source
NER pass has no long-running, interruptible phase analogous to the multi-pass
LLM summary/revision generation — cancellation is honored only at entry
(before ``execute`` begins), not mid-flight. The unit runs in-process and
cannot be force-terminated once started, so ``cancel`` records the request and
the runner resolves ``CANCELLED`` / ``TIMED_OUT`` from its own slot state.
A cancel that lands after execute entry does not prevent the document's run
and mentions from committing — the ``CANCELLED`` status marks the unit's
lifecycle, not the absence of produced data.
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


def _utcnow() -> datetime.datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc)


def _error_code(exc: BaseException) -> str:
    """Classify an exception into a short, stable error code for the audit row."""
    if isinstance(exc, ValueError):
        return "unprocessable_input"
    return type(exc).__name__


def _error_message(exc: BaseException) -> str:
    """Return a stable, scrubbed category message for the audit row and operator API.

    Raw exception text (model or DB errors) may embed identifiers or SQL
    fragments; it is persisted into ``error_message`` and returned by the
    operator API, so it must not carry that detail across the boundary. Only
    this category text is persisted/returned — the precise stable classifier
    stays in :func:`_error_code`, and the full exception (with traceback) is
    logged internally in :meth:`ExtractionStageHandler.execute` for diagnosis.
    """
    if isinstance(exc, ValueError):
        return "input could not be processed"
    return "a transient processing error occurred"


class ExtractionStageHandler:
    """Drive extraction work-units through the generic pipeline runner.

    The work-unit handle is the integer ``ExtractionJob.id``. The NER
    extractor and input policy are injected dependencies, so a deterministic
    substitute extractor drives the stage in tests with no change to this
    logic.
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
        """Store the engine, injected dependencies, and runner-facing configuration.

        Args:
            engine: The shared SQLite engine; the unit-of-work
                (``extract_document``) opens its own short ``BEGIN IMMEDIATE``
                for the domain writes (after the lock-free read/extract phase).
            extractor: The single injected NER access point.
            input_policy: The raw-vs-contextualized input toggle every
                extraction under this handler runs with.
            timeout_seconds: Wall-clock timeout per unit (the runner enforces it).
            concurrency: Maximum units the runner may execute simultaneously.
            stale_after_minutes: Minutes in ``RUNNING`` before recovery reclaims a
                unit.
            retry_base_delay_seconds: Retry backoff base; wait is
                ``base * 2**attempts``.
            retry_max_attempts: Attempt cap after which a retryable failure
                becomes permanent.
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
        """A no-op: dependency gating happens by eager extractor construction in the composition root.

        :func:`aizk.graph.extraction_worker.build_extractor` constructs the
        pinned extractor before the runner starts; a missing dependency or
        model artifact raises :class:`ImportError` there, which the CLI maps
        to a startup failure. By the time this handler exists, its one
        external dependency is already proven constructible.
        """
        return None

    def scope_id(self, handle: int) -> str:
        """Return the run ``scope_id`` for ``handle``: the source ``source_id``.

        Reads the work-unit's durable source identity (runs are scoped per
        source, not per work-unit). Returns the bare handle as a fallback when
        the row is missing, so correlation logging never raises.
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
        """Map an execution result or exception to a terminal outcome.

        A successful result maps to ``SUCCEEDED``. A :class:`ValueError` — the
        type raised for unprocessable input (an unchunked source, or an
        invalid ``input_policy``) — maps to a ``permanent`` ``FAILED``; any
        other exception (a transient model or I/O error) maps to a
        ``retryable`` ``FAILED``. ``TIMED_OUT`` and ``CANCELLED`` are resolved
        by the runner from its slot state for in-process units and do not pass
        through here.
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
        """Claim the oldest eligible work-unit, transition it to ``RUNNING``, or ``None``.

        Runs inside the runner-owned ``BEGIN IMMEDIATE`` transaction: selects the
        oldest (by ``queued_at``) unit that is ``QUEUED`` or a retryable
        ``FAILED`` whose retry-wait has elapsed, transitions it to ``RUNNING``,
        post-increments ``attempts`` (the claim is the attempt counter's source of
        truth), and records the ``claimed`` event. Returns the claimed id, or
        ``None`` when none is eligible. Does not commit.
        """
        now = _utcnow()
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
        """Reset units stranded in ``RUNNING`` past the stale threshold to eligible.

        Runs inside the runner-owned transaction: reclaims ``RUNNING`` units whose
        ``started_at`` is older than ``stale_after_minutes`` to ``FAILED`` with an
        immediate retry-wait (so the next claim re-runs them), recording a
        ``recovered_stale`` event for each. Does not increment ``attempts`` (a
        stale reset is not an attempt) and does not commit. Returns the reset ids.
        """
        now = _utcnow()
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
        """Run the unit-of-work for ``handle``; bridge result/error details.

        Resolves the work-unit's source identity, honors an entry-time
        cancellation, then runs :func:`~aizk.graph.extraction_run.extract_document`.
        Stashes the extraction run id + mention count for the ``SUCCEEDED``
        event; on any :class:`Exception` stashes the scrubbed error code +
        message for the ``FAILED`` event and re-raises so ``map_result``
        classifies it.

        Raises:
            ValueError: If the work-unit row is missing, the source has no
                active chunking run, or ``input_policy`` is invalid
                (unprocessable input — see :func:`~aizk.graph.extraction_run.extract_document`).
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
                source_id=str(source_id),
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
        """Write the work-unit's terminal status + event into the runner's transaction.

        Runs inside the runner-owned ``BEGIN IMMEDIATE`` transaction and does not
        commit. The success terminal status is written here (the domain writes
        committed separately in :meth:`execute`); the ``SUCCEEDED`` event carries
        the extraction ``run_id``. A ``FAILED`` outcome is retryable (an immediate-
        backoff retry-wait, re-eligible) only while ``attempts`` is below the cap;
        otherwise it is permanent (``earliest_next_attempt_at`` cleared,
        ``finished_at`` set). ``CANCELLED`` / ``TIMED_OUT`` are recorded terminal.

        Reads the execute→finalize bridges without clearing them: the runner retains
        the slot and re-invokes finalize on a finalize-time DB error, so clearing
        here would make the retry record ``run_id=None`` or replace the real
        scrubbed error with the unknown-error fallback. :meth:`cleanup` releases the
        bridges, once, only after the durable commit.
        """
        with self._lock:
            success = self._success.get(handle)
            error = self._errors.get(handle)

        job = session.get(ExtractionJob, handle)
        if job is None:
            return
        now = _utcnow()
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

        # WorkUnitStatus.FAILED — retry class is required on the outcome.
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
        """Return ``True`` if cancellation has been requested for ``handle``, from either source.

        Consults two places so a cancel from either path stops in-flight work:

        - the in-process request set — the runner's cooperative cancel and
          wall-clock-timeout enforcement, set within this process; and
        - the **durable** work-unit status — an operator cancel committed by the
          API, typically in a *different* process, which the in-process set would
          never observe.

        Checked only at entry (before :func:`~aizk.graph.extraction_run.extract_document`
        runs), not mid-flight, since that function accepts no cancellation
        callback (see the module docstring).
        """
        with self._lock:
            if handle in self._cancelled:
                return True
        with Session(self._engine) as session:
            job = session.get(ExtractionJob, handle)
            return job is not None and job.status is WorkUnitStatus.CANCELLED
