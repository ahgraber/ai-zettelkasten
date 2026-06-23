"""The contextualization stage's :class:`~aizk.pipeline.handler.StageHandler`.

This adapter exposes the graph stage's per-document unit-of-work (chunk-persist
and contextualize one converted document, keyed by its integer
``ContextualizationJob.id``) to the generic pipeline runner. The runner owns the
claim/lease loop, bounded concurrency, wall-clock timeout, stale-recovery
scheduling, and the ``BEGIN IMMEDIATE`` transaction around ``claim_next`` /
``recover_stale`` / ``finalize``; this handler owns the graph-specific surface:
the claim/recovery queries, the unit-of-work execution, result classification,
the run ``scope_id`` (the source ``source_id``), and the in-process
timeout/concurrency declarations.

Transaction ownership and at-least-once (per the change's design):

- ``claim_next`` / ``recover_stale`` / ``finalize`` run inside the runner-owned
  transaction and never commit — their work-unit status transitions co-commit
  with the runner's bookkeeping.
- ``execute`` runs **outside** that transaction and opens its **own**
  ``BEGIN IMMEDIATE`` to commit the domain writes (the chunking, summary, and
  variant runs with their rows). The work-unit's terminal transition therefore
  commits separately from the domain writes; if the process dies between them
  the unit is recovered (``recover_stale``) and ``execute`` re-runs. Re-execution
  is safe because :func:`aizk.graph.workunit.process_document` is idempotent on
  its derivation keys — an unchanged document reuses its active runs and produces
  no duplicate chunk, summary, or variant.

The work-unit's transition events carry the ``source_id`` source identity and,
on the terminal success event, the ``run_id`` of the work-unit's chunking run
(the root of the backward-trace chain), so a source's progress is resolvable
across stages.

Cancellation is cooperative and best-effort: the unit runs in-process and cannot
be force-terminated, so ``cancel`` records the request and the runner resolves
``CANCELLED`` / ``TIMED_OUT`` from its own slot state. A per-document unit is
short, so it is not interrupted mid-flight.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
from typing import TYPE_CHECKING

from sqlalchemy import or_
from sqlmodel import Session, select

from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.events import (
    CONTEXTUALIZATION_STAGE,
    CancelledPayload,
    ClaimedPayload,
    FailedPayload,
    GraphEventKind,
    RecoveredStalePayload,
    SkippedSupersededPayload,
    SucceededPayload,
    TimedOutPayload,
)
from aizk.graph.workunit import ProcessResult, SkippedSuperseded, process_document
from aizk.pipeline.events import record_transition
from aizk.pipeline.handler import Isolation, StageResult
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from aizk.graph.llm import LLMClient
    from aizk.graph.workunit import MarkdownSource, OutputFreshness

logger = logging.getLogger(__name__)

#: Default wall-clock timeout for one document's contextualization unit-of-work.
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

    Raw exception text (provider, S3, or DB errors) may embed object keys,
    endpoints, or SQL fragments; it is persisted into ``error_message`` and
    returned by the operator API, so it must not carry that detail across the
    boundary. Only this category text is persisted/returned — the precise stable
    classifier stays in :func:`_error_code`, and the full exception (with
    traceback) is logged internally in :meth:`ContextualizationStageHandler.execute`
    for diagnosis.
    """
    if isinstance(exc, ValueError):
        return "input could not be processed"
    return "a transient processing error occurred"


class ContextualizationStageHandler:
    """Drive contextualization work-units through the generic pipeline runner.

    The work-unit handle is the integer ``ContextualizationJob.id``. The model
    and Markdown source are injected dependencies, so a deterministic substitute
    drives the stage in tests with no change to this logic.
    """

    def __init__(
        self,
        engine: "Engine",
        llm_client: "LLMClient",
        markdown_source: "MarkdownSource",
        freshness: "OutputFreshness",
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES,
        retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS,
    ) -> None:
        """Store the engine, injected dependencies, and runner-facing configuration.

        Args:
            engine: The shared SQLite engine; the unit-of-work opens its own short
                ``BEGIN IMMEDIATE`` for the domain writes (after the lock-free
                fetch/generate phase).
            llm_client: The single model access point for the summary and variant
                passes.
            markdown_source: The seam that fetches a document's Markdown by
                ``conversion_output_id``.
            freshness: The seam deciding whether a conversion output is still the
                source's latest (the monotonic-currentness gate).
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
        self._llm_client = llm_client
        self._markdown_source = markdown_source
        self._freshness = freshness
        self._timeout_seconds = timeout_seconds
        self._concurrency = concurrency
        self._stale_after_minutes = stale_after_minutes
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_attempts = retry_max_attempts
        # Per-handle bridges across the execute/finalize boundary (finalize is
        # handed only (session, handle, outcome)): the produced chunking run id +
        # variant count for the SUCCEEDED event, the superseding-output id for a
        # SKIPPED_SUPERSEDED event, and the scrubbed error code + message for a
        # FAILED event. Guarded by one lock (N worker threads race). finalize reads
        # them without clearing; cleanup() releases them exactly once, only after
        # the durable terminal transition commits — so a runner finalize-retry (on a
        # finalize-time DB error, slot retained) still sees them.
        self._success: dict[int, tuple[int, int]] = {}
        self._skipped: dict[int, int] = {}
        self._errors: dict[int, tuple[str, str]] = {}
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()

    @property
    def stage(self) -> str:
        """Return the stable stage name used in the correlation spine."""
        return CONTEXTUALIZATION_STAGE

    def validate_dependencies(self) -> None:
        """No external probes: the model and Markdown source are injected dependencies."""
        return None

    def scope_id(self, handle: int) -> str:
        """Return the run ``scope_id`` for ``handle``: the source ``source_id``.

        Reads the work-unit's durable source identity (runs are scoped per
        source, not per work-unit). Returns the bare handle as a fallback when
        the row is missing, so correlation logging never raises.
        """
        with Session(self._engine) as session:
            job = session.get(ContextualizationJob, handle)
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
        """Return the execution isolation: contextualization runs in-process."""
        return Isolation.IN_PROCESS

    def map_result(self, result_or_exc: StageResult | BaseException) -> TerminalOutcome:
        """Map an execution result or exception to a terminal outcome.

        A successful result maps to ``SUCCEEDED``. A :class:`ValueError` — the
        type raised for a guardrail rejection (over-budget summary/revision) or
        unprocessable input (markdown-hash drift, provenance mismatch) — maps to
        a ``permanent`` ``FAILED``; any other exception (a transient model or I/O
        error) maps to a ``retryable`` ``FAILED``. ``TIMED_OUT`` and ``CANCELLED``
        are resolved by the runner from its slot state for in-process units and
        do not pass through here.
        """
        if isinstance(result_or_exc, BaseException):
            if isinstance(result_or_exc, ValueError):
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
            select(ContextualizationJob)
            .where(
                or_(
                    ContextualizationJob.status == WorkUnitStatus.QUEUED,
                    (ContextualizationJob.status == WorkUnitStatus.FAILED)
                    & ContextualizationJob.earliest_next_attempt_at.is_not(None)  # type: ignore[union-attr]
                    & (ContextualizationJob.earliest_next_attempt_at <= now),
                )
            )
            .order_by(ContextualizationJob.queued_at)
        ).first()
        if job is None:
            return None

        job.started_at = now
        job.attempts += 1
        job.updated_at = now
        record_transition(
            session,
            job,
            stage=CONTEXTUALIZATION_STAGE,
            work_unit_ref=str(job.id),
            source_id=job.source_id,
            to_status=WorkUnitStatus.RUNNING,
            kind=GraphEventKind.CLAIMED,
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
            select(ContextualizationJob)
            .where(ContextualizationJob.status == WorkUnitStatus.RUNNING)
            .where(ContextualizationJob.started_at.is_not(None))  # type: ignore[union-attr]
            .where(ContextualizationJob.started_at < stale_before)
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
                stage=CONTEXTUALIZATION_STAGE,
                work_unit_ref=str(job.id),
                source_id=job.source_id,
                to_status=WorkUnitStatus.FAILED,
                kind=GraphEventKind.RECOVERED_STALE,
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
        cancellation, then runs :func:`~aizk.graph.workunit.process_document`
        (which keeps the S3 fetch + LLM passes outside its own short
        ``BEGIN IMMEDIATE`` write transaction). Stashes the chunking run id +
        variant count for the ``SUCCEEDED`` event, or the superseding-output id for
        a ``SKIPPED_SUPERSEDED`` event; on any :class:`Exception` stashes the
        scrubbed error code + message for the ``FAILED`` event and re-raises so
        ``map_result`` classifies it.

        Raises:
            ValueError: If the work-unit row is missing (unprocessable input).
        """
        with Session(self._engine) as session:
            job = session.get(ContextualizationJob, handle)
            if job is None:
                raise ValueError(f"contextualization work-unit {handle} is missing")
            # An operator may have cancelled the unit before execution started; do
            # no work (finalize honors the terminal CANCELLED).
            if job.status is WorkUnitStatus.CANCELLED:
                logger.info("contextualization work-unit %s already cancelled; skipping execution", handle)
                return None
            source_id = job.source_id
            conversion_output_id = job.conversion_output_id

        # A runner cancel/timeout already requested before work started: skip.
        if self._is_cancelled(handle):
            logger.info("contextualization work-unit %s cancelled before start; skipping execution", handle)
            return None

        try:
            result = process_document(
                self._engine,
                self._llm_client,
                source_id=source_id,
                conversion_output_id=conversion_output_id,
                markdown_source=self._markdown_source,
                freshness=self._freshness,
                is_cancelled=lambda: self._is_cancelled(handle),
            )
        except Exception as exc:
            # Full detail (including the raw message) stays in the internal log; only
            # the stable code + scrubbed category message cross into the audit row/API.
            logger.exception("contextualization work-unit %s failed during execution", handle)
            with self._lock:
                self._errors[handle] = (_error_code(exc), _error_message(exc))
            raise

        with self._lock:
            if isinstance(result, ProcessResult):
                self._success[handle] = (result.chunking_run_id, result.variant_count)
            elif isinstance(result, SkippedSuperseded):
                self._skipped[handle] = result.conversion_output_id
            # A Cancelled result writes nothing; the runner resolves the terminal
            # CANCELLED / TIMED_OUT status from its slot state and finalize records it.
        return result

    def finalize(self, session: "Session", handle: int, outcome: TerminalOutcome) -> None:
        """Write the work-unit's terminal status + event into the runner's transaction.

        Runs inside the runner-owned ``BEGIN IMMEDIATE`` transaction and does not
        commit. The success terminal status is written here (the domain writes
        committed separately in :meth:`execute`); the ``SUCCEEDED`` event carries
        the chunking ``run_id``. A ``FAILED`` outcome is retryable (an immediate-
        backoff retry-wait, re-eligible) only while ``attempts`` is below the cap;
        otherwise it is permanent (``earliest_next_attempt_at`` cleared,
        ``finished_at`` set). ``CANCELLED`` / ``TIMED_OUT`` are recorded terminal.

        Reads the execute→finalize bridges without clearing them: the runner retains
        the slot and re-invokes finalize on a finalize-time DB error, so clearing
        here would make the retry record ``run_id=None``, lose the skipped-superseded
        distinction, or replace the real scrubbed error with the unknown-error
        fallback. :meth:`cleanup` releases the bridges, once, only after the durable
        commit.
        """
        with self._lock:
            success = self._success.get(handle)
            skipped_output_id = self._skipped.get(handle)
            error = self._errors.get(handle)

        job = session.get(ContextualizationJob, handle)
        if job is None:
            return
        now = _utcnow()
        status = outcome.status

        if status is WorkUnitStatus.SUCCEEDED:
            # An operator cancel that landed while the unit ran wins: do not
            # overwrite a terminal CANCELLED with SUCCEEDED.
            if job.status is WorkUnitStatus.CANCELLED:
                logger.info("contextualization work-unit %s was cancelled; not overwriting with succeeded", handle)
                return
            job.finished_at = now
            job.updated_at = now
            job.error_code = None
            job.error_message = None
            if skipped_output_id is not None:
                # A newer conversion output won; the unit did nothing and succeeds
                # as a no-op, recorded distinctly in the audit log.
                record_transition(
                    session,
                    job,
                    stage=CONTEXTUALIZATION_STAGE,
                    work_unit_ref=str(job.id),
                    source_id=job.source_id,
                    to_status=WorkUnitStatus.SUCCEEDED,
                    kind=GraphEventKind.SKIPPED_SUPERSEDED,
                    attempt=job.attempts,
                    payload=SkippedSupersededPayload(superseding_output_id=None),
                )
                return
            run_id, variant_count = success if success is not None else (None, 0)
            record_transition(
                session,
                job,
                stage=CONTEXTUALIZATION_STAGE,
                work_unit_ref=str(job.id),
                source_id=job.source_id,
                to_status=WorkUnitStatus.SUCCEEDED,
                kind=GraphEventKind.SUCCEEDED,
                attempt=job.attempts,
                run_id=run_id,
                payload=SucceededPayload(variant_count=variant_count),
            )
            return

        if status is WorkUnitStatus.CANCELLED:
            job.finished_at = now
            job.updated_at = now
            record_transition(
                session,
                job,
                stage=CONTEXTUALIZATION_STAGE,
                work_unit_ref=str(job.id),
                source_id=job.source_id,
                to_status=WorkUnitStatus.CANCELLED,
                kind=GraphEventKind.CANCELLED,
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
                stage=CONTEXTUALIZATION_STAGE,
                work_unit_ref=str(job.id),
                source_id=job.source_id,
                to_status=WorkUnitStatus.TIMED_OUT,
                kind=GraphEventKind.TIMED_OUT,
                attempt=job.attempts,
                payload=TimedOutPayload(timeout_seconds=self._timeout_seconds),
            )
            return

        # WorkUnitStatus.FAILED — retry class is required on the outcome.
        error_code, error_message = error if error is not None else ("contextualization_error", "unknown error")
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
            stage=CONTEXTUALIZATION_STAGE,
            work_unit_ref=str(job.id),
            source_id=job.source_id,
            to_status=WorkUnitStatus.FAILED,
            kind=GraphEventKind.FAILED,
            attempt=job.attempts,
            payload=FailedPayload(error_code=error_code, error_message=error_message, retryable=retryable),
        )

    def cleanup(self, handle: int) -> None:
        """Release the unit's per-handle bridge entries; called on every outcome."""
        with self._lock:
            self._success.pop(handle, None)
            self._skipped.pop(handle, None)
            self._errors.pop(handle, None)
            self._cancelled.discard(handle)

    def cancel(self, handle: int) -> None:
        """Record a cooperative cancellation request for ``handle``.

        The unit runs in-process and cannot be force-terminated, but the request
        is honored cooperatively: :meth:`execute` checks it at entry and passes it
        into the unit-of-work, which skips the model passes and/or the persist
        write rather than committing work for a cancelled unit. The runner
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

        The unit-of-work calls this before its persist write (inside its own
        ``BEGIN IMMEDIATE``), so a unit found ``CANCELLED`` in the store skips the
        domain write rather than committing runs for a cancelled unit. The status
        read runs on a separate connection; under WAL it never blocks on the
        unit-of-work's in-flight write lock.
        """
        with self._lock:
            if handle in self._cancelled:
                return True
        with Session(self._engine) as session:
            job = session.get(ContextualizationJob, handle)
            return job is not None and job.status is WorkUnitStatus.CANCELLED
