"""The pipeline-stage runner: the current embedded orchestration engine.

It drives any stage that supplies a
:class:`~aizk.pipeline.handler.StageHandler` over its own store and owns
the engine-side concerns:

- the claim/lease loop and bounded concurrency over a thread pool;
- eligibility + submission ordering (delegated to the handler's claim query,
  driven one-claim-per-slot in claim order). "Begin in submission order" is a
  *claim/dispatch*-order guarantee — eligible units are claimed (selected) FIFO
  by submission, with no starvation — not a guarantee about worker-thread start
  timing: once dispatched to the pool, threads may begin in any order, and that
  is expected under bounded concurrency;
- retry-wait gating (the handler's eligibility query; the runner simply
  re-polls);
- signal handling and graceful drain within a bounded timeout;
- wall-clock timeout enforcement per unit, graceful-before-forceful termination
  for subprocess-isolated stages, cooperative cancellation + cleanup for
  in-process stages;
- transient-resource cleanup on *every* terminal outcome;
- stale-unit recovery scheduling;
- startup dependency validation gating work acceptance; and
- lifecycle observability (structured logs + metrics + ``setproctitle``
  stage-role identification).

It puts **no** stage-specific knowledge inside itself. The execution model is
**thread-pool + optional subprocess**, not asyncio (see design decision
``ExecutionModelIsThreadPlusOptionalSubprocess``).

Transaction discipline: the runner owns the DB session and the
``BEGIN IMMEDIATE`` transaction. It passes the session into ``claim_next`` /
``recover_stale`` / ``finalize`` so the handler's status transition (routed
through :func:`aizk.pipeline.events.record_transition`) co-commits with the
runner's bookkeeping; the handler never commits.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Generic, NoReturn, Protocol

from setproctitle import setproctitle
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlmodel import Session

from aizk.pipeline import shutdown as shutdown_module
from aizk.pipeline.handler import StageHandler, WorkUnitHandle
from aizk.pipeline.lifecycle import TerminalOutcome, WorkUnitStatus
from aizk.pipeline.shutdown import ShutdownController

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

logger = logging.getLogger(__name__)


class StageMetrics(Protocol):
    """Operational-metrics sink the runner emits lifecycle counters through.

    A minimal counter/gauge interface so the runner stays decoupled from any
    specific metrics backend. The default :class:`InMemoryMetrics` records
    counts in-process; production wires a real backend with the same shape.
    """

    def increment(self, name: str, *, tags: dict[str, str] | None = None) -> None:
        """Increment the counter ``name`` by one, optionally tagged."""
        ...

    def gauge(self, name: str, value: int, *, tags: dict[str, str] | None = None) -> None:
        """Record gauge ``name`` at ``value``, optionally tagged."""
        ...


class InMemoryMetrics:
    """A thread-safe in-process :class:`StageMetrics` for tests and embedding."""

    def __init__(self) -> None:
        """Initialize empty counter/gauge stores."""
        self.counters: dict[str, int] = {}
        self.counter_tags: dict[str, dict[str, str]] = {}
        self.gauges: dict[str, int] = {}
        self._lock = threading.Lock()

    def increment(self, name: str, *, tags: dict[str, str] | None = None) -> None:
        """Increment counter ``name`` by one, retaining the tags it was given."""
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + 1
            if tags is not None:
                self.counter_tags[name] = dict(tags)

    def gauge(self, name: str, value: int, *, tags: dict[str, str] | None = None) -> None:
        """Record gauge ``name`` at ``value``."""
        with self._lock:
            self.gauges[name] = value


# Clock seam: returns monotonic seconds. Patchable so timeout/drain tests do not
# rely on wall-clock sleeps longer than necessary.
Clock = Callable[[], float]


class _Slot(Generic[WorkUnitHandle]):
    """Bookkeeping for one in-flight work-unit occupying a concurrency slot."""

    __slots__ = ("handle", "future", "deadline", "timed_out", "cancel_requested")

    def __init__(self, handle: WorkUnitHandle, future: Future, deadline: float | None) -> None:
        self.handle = handle
        self.future = future
        self.deadline = deadline
        self.timed_out = False
        self.cancel_requested = False


class StageRunner(Generic[WorkUnitHandle]):
    """Drives a single :class:`StageHandler` through the generic lifecycle.

    One runner instance owns one stage's loop. Two stages share a process by
    constructing two runners (each with its own handler, engine, and
    :class:`ShutdownController`); no runner state is module-global.

    Concurrency invariant: the claim/reap/deadline-enforcement loop body runs on
    a single driver thread, which is the sole mutator of ``_slots`` and of each
    slot's ``timed_out`` / ``deadline`` fields. ``_slots_lock`` guards the
    ``_slots`` dict and the one cross-thread write — ``cancel_requested``, set by
    :meth:`cancel_handle` from a foreign thread — read by the driver thread.
    """

    def __init__(
        self,
        handler: StageHandler[WorkUnitHandle],
        engine: "Engine",
        *,
        shutdown: ShutdownController | None = None,
        metrics: StageMetrics | None = None,
        drain_timeout: float = 30.0,
        poll_interval: float = 2.0,
        stale_recovery_interval: float = 60.0,
        cancel_grace: float = 5.0,
        clock: Clock = time.monotonic,
        force_exit: Callable[[int], object] = shutdown_module.force_exit,
    ) -> None:
        """Construct a runner over ``handler`` backed by ``engine``.

        Args:
            handler: The stage adapter the runner drives.
            engine: SQLite engine the runner opens ``BEGIN IMMEDIATE``
                transactions on for claim / finalize / recover.
            shutdown: Per-instance shutdown controller; one is created if omitted.
            metrics: Operational-metrics sink; an :class:`InMemoryMetrics` is
                created if omitted.
            drain_timeout: Seconds to allow in-flight units to finish on
                shutdown before forcing exit.
            poll_interval: Seconds between claim attempts when no slot was filled.
            stale_recovery_interval: Seconds between stale-unit recovery sweeps.
            cancel_grace: Seconds to wait — only on the drain-timeout survivor
                path — for a cancelled unit to finish before the runner gives up
                and forces exit. Not a general per-unit escalation timer;
                subprocess forceful termination lives in the adapter's ``cancel``.
            clock: Monotonic-seconds source (seam for deterministic timing tests).
            force_exit: Process-exit hook invoked on a forced drain; a patchable
                seam (default :func:`aizk.pipeline.shutdown.force_exit`) so tests
                can assert forced exit without a real ``os._exit``.
        """
        self._handler = handler
        self._engine = engine
        self._shutdown = shutdown or ShutdownController()
        self._metrics = metrics or InMemoryMetrics()
        self._drain_timeout = drain_timeout
        self._poll_interval = poll_interval
        self._stale_recovery_interval = stale_recovery_interval
        self._cancel_grace = cancel_grace
        self._clock = clock
        self._force_exit = force_exit
        self._stage = handler.stage
        self._dependencies_validated = False
        self._slots: dict[Future, _Slot[WorkUnitHandle]] = {}
        self._slots_lock = threading.Lock()
        self._last_recovery = 0.0

    @property
    def metrics(self) -> StageMetrics:
        """The operational-metrics sink this runner emits through."""
        return self._metrics

    @property
    def shutdown(self) -> ShutdownController:
        """The per-instance shutdown controller."""
        return self._shutdown

    # --- transaction helper ---------------------------------------------------

    @contextmanager
    def _begin_immediate(self) -> "Iterator[Session]":
        """Open a ``BEGIN IMMEDIATE`` session; commit on success, roll back on error.

        Mirrors conversion's "caller owns BEGIN IMMEDIATE, helper does not
        commit" convention: the handler's transition writes are staged inside
        this block and committed here, co-committed with the runner bookkeeping.
        """
        session = Session(self._engine)
        try:
            session.exec(text("BEGIN IMMEDIATE"))
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # --- startup --------------------------------------------------------------

    def validate_dependencies(self) -> None:
        """Run the handler's startup dependency validation, gating acceptance.

        Raises whatever the handler raises. Until this completes
        successfully, :meth:`run` / :meth:`run_until_idle` refuse to claim work.
        """
        logger.info("Validating dependencies", extra={"stage": self._stage})
        self._handler.validate_dependencies()
        self._dependencies_validated = True
        self._metrics.increment("pipeline.startup.validated", tags={"stage": self._stage})
        logger.info("Dependencies validated", extra={"stage": self._stage})

    def set_process_title(self) -> None:
        """Advertise this runner's stage role in the process title for operators.

        Multi-runner semantics: the process title is a single process-global
        slot, so ``setproctitle`` is last-write-wins in a shared process. When
        two runners share a process, the most recently set title is the one
        operators see; the title is an advisory role hint, not a per-runner
        identifier.
        """
        try:
            setproctitle(f"aizk-stage-{self._stage}")
        except Exception:  # pragma: no cover - setproctitle is best-effort
            logger.debug("setproctitle unavailable; skipping process-title update", exc_info=True)

    # --- claim / recover ------------------------------------------------------

    def recover_stale(self) -> int:
        """Run one stale-unit recovery sweep; return the number recovered.

        Reclaims units stranded in ``running`` by an interrupted runtime,
        recording each recovery's cause in its transition event (the handler
        owns the cause). A legitimate operational entry point (e.g. a startup
        sweep), in addition to being scheduled periodically inside :meth:`run`.
        """
        return self._recover_stale()

    def _recover_stale(self) -> int:
        """Run one stale-unit recovery sweep; return the number recovered."""
        try:
            with self._begin_immediate() as session:
                recovered = self._handler.recover_stale(session)
        except (OperationalError, DBAPIError):
            logger.warning("Stale recovery skipped due to database error", exc_info=True)
            return 0
        if recovered:
            logger.warning(
                "Recovered %d stale running work-units",
                len(recovered),
                extra={"stage": self._stage, "recovered": len(recovered)},
            )
            for _ in recovered:
                self._metrics.increment("pipeline.work_unit.recovered", tags={"stage": self._stage})
        return len(recovered)

    def _claim_next(self) -> WorkUnitHandle | None:
        """Claim one eligible unit inside a ``BEGIN IMMEDIATE`` transaction.

        Returns the opaque handle, or ``None`` when none is eligible or the
        store is locked. The eligibility query (submission order + retry-wait
        gating) lives in the handler; the runner only drives it. The spec's
        "begin in submission order" is this claim/selection order — units are
        dispatched oldest-first; the runner does not serialize the worker
        threads' actual start instants (they run concurrently up to the limit).
        """
        try:
            with self._begin_immediate() as session:
                handle = self._handler.claim_next(session)
        except OperationalError:
            logger.warning("Claim skipped due to database lock", exc_info=True)
            return None
        except DBAPIError:
            logger.exception("Claim failed due to database error")
            return None
        if handle is not None:
            self._metrics.increment("pipeline.work_unit.claimed", tags={"stage": self._stage})
            # aizk_uuid / run_id correlation lives on the handler's
            # pipeline_events rows (narrow seam), not in the runner's logs.
            logger.info(
                "Claimed work-unit",
                extra={"stage": self._stage, "work_unit_ref": str(handle)},
            )
        return handle

    # --- execution ------------------------------------------------------------

    def _run_unit(self, handle: WorkUnitHandle, registered: threading.Event) -> Any:
        """Worker-thread body: execute the unit-of-work and return its result.

        Waits until the runner has registered this unit's concurrency slot
        before running, so cancellation and deadline enforcement can always find
        the slot once ``execute`` has begun (closing the submit/register race).
        Exceptions propagate to the future; the completion path maps them to a
        terminal outcome. Keeps no runner state — pure call into the adapter.
        """
        registered.wait()
        return self._handler.execute(handle)

    def _submit(self, executor: ThreadPoolExecutor, handle: WorkUnitHandle) -> None:
        """Submit a claimed unit to the pool and register its concurrency slot.

        The slot is registered before the worker is allowed to start executing
        (via the ``registered`` gate) so an in-flight unit is always visible to
        :meth:`cancel_handle` and :meth:`_enforce_deadlines`. The pool's
        ``max_workers`` (the handler's ``concurrency_limit``) bounds
        concurrency; the gate is purely for slot-visibility, not bounding.
        """
        now = self._clock()
        timeout_seconds = self._handler.timeout.total_seconds()
        deadline = now + timeout_seconds if timeout_seconds > 0 else None
        registered = threading.Event()
        future = executor.submit(self._run_unit, handle, registered)
        with self._slots_lock:
            self._slots[future] = _Slot(handle, future, deadline)
            in_flight = len(self._slots)
        registered.set()
        self._metrics.increment("pipeline.work_unit.started", tags={"stage": self._stage})
        self._metrics.gauge("pipeline.work_unit.in_flight", in_flight, tags={"stage": self._stage})

    def _enforce_deadlines(self) -> None:
        """Cancel any in-flight unit past its wall-clock deadline.

        In-process units are cooperatively cancelled (the adapter's ``cancel``
        signals the work to stop). Subprocess-isolated units are terminated by
        the adapter's ``cancel`` (graceful-before-forceful, killing the process
        group so no descendant is orphaned). Either way the slot is marked
        ``timed_out`` so the completion path records the timed-out outcome.
        """
        now = self._clock()
        with self._slots_lock:
            slots = list(self._slots.values())
        for slot in slots:
            if slot.deadline is None or slot.timed_out or slot.future.done():
                continue
            if now >= slot.deadline:
                slot.timed_out = True
                slot.cancel_requested = True
                logger.warning(
                    "Work-unit exceeded wall-clock timeout; terminating",
                    extra={"stage": self._stage, "work_unit_ref": str(slot.handle)},
                )
                self._request_cancel(slot.handle)

    def _request_cancel(self, handle: WorkUnitHandle) -> None:
        """Invoke the adapter's cooperative/forceful cancellation hook."""
        try:
            self._handler.cancel(handle)
        except Exception:
            logger.exception(
                "Cancellation hook raised",
                extra={"stage": self._stage, "work_unit_ref": str(handle)},
            )

    def cancel_handle(self, handle: WorkUnitHandle) -> None:
        """Request cancellation of a running unit identified by ``handle``.

        Looks the handle up among in-flight slots, marks it cancel-requested,
        and invokes the adapter's cancellation hook. A no-op if the handle is
        not currently in flight.
        """
        with self._slots_lock:
            target = next((slot for slot in self._slots.values() if slot.handle == handle), None)
            if target is not None:
                # The one cross-thread flag write — set under the lock the driver reads it through.
                target.cancel_requested = True
        if target is None:
            return
        self._request_cancel(handle)

    def _reap(self) -> None:
        """Finalize completed (and cancel-grace-expired) slots, releasing them.

        For each finished future: map its result/exception to a terminal
        outcome (timed-out / cancelled override the adapter's mapping), finalize
        the status transition in a ``BEGIN IMMEDIATE`` transaction, and run
        ``cleanup`` on every outcome. Slots whose cancel grace elapsed without
        the future finishing are escalated again via ``cancel``.
        """
        with self._slots_lock:
            slots = list(self._slots.items())
        for future, slot in slots:
            if not future.done():
                continue
            self._finalize_slot(future, slot)

    def _finalize_slot(self, future: Future, slot: _Slot[WorkUnitHandle]) -> None:
        """Finalize one finished slot, releasing it only after the durable transition commits.

        The slot is the runner's local record that this unit ran; it is dropped
        — and ``cleanup`` plus the outcome metric run — **only after** the
        terminal status transition has durably committed. On a finalize DB error
        the slot is left in place (no pop, no cleanup, no outcome metric): the
        unit stays known to the runner so the next :meth:`_reap` pass re-attempts
        ``finalize`` (the future is already done, so :meth:`_resolve_outcome`
        re-reads it idempotently). Retries occur on the normal reap/poll cadence,
        never in a tight loop, and ``cleanup`` ultimately runs exactly once — on
        the successful terminal commit. Dropping the slot before the durable
        transition landed would strand the unit ``running`` in the store while
        the runner forgot it, inviting delayed stale-recovery and duplicate
        execution.
        """
        handle = slot.handle
        outcome = self._resolve_outcome(future, slot)
        try:
            with self._begin_immediate() as session:
                self._handler.finalize(session, handle, outcome)
        except (OperationalError, DBAPIError):
            logger.exception(
                "Finalize failed due to database error; leaving slot for retry on next reap",
                extra={"stage": self._stage, "work_unit_ref": str(handle)},
            )
            return  # durable transition not recorded — keep the slot; retry next reap
        # Durable terminal transition committed: now release the slot exactly once.
        self._emit_outcome_metric(outcome)
        self._cleanup(handle)
        with self._slots_lock:
            self._slots.pop(future, None)
            self._metrics.gauge(
                "pipeline.work_unit.in_flight",
                len(self._slots),
                tags={"stage": self._stage},
            )

    def _resolve_outcome(self, future: Future, slot: _Slot[WorkUnitHandle]) -> TerminalOutcome:
        """Determine the terminal outcome for a finished slot.

        Timeout and cancellation are runner-determined and take precedence over
        the adapter's result mapping; otherwise the adapter maps the result or
        exception.
        """
        if slot.timed_out:
            return TerminalOutcome(WorkUnitStatus.TIMED_OUT)
        if slot.cancel_requested:
            return TerminalOutcome(WorkUnitStatus.CANCELLED)
        # The runner never calls ``future.cancel()``, so ``result()`` below cannot
        # raise ``CancelledError`` on this finished, non-timed-out, non-cancelled future.
        exc = future.exception()
        if exc is not None:
            return self._handler.map_result(exc)
        return self._handler.map_result(future.result())

    def _emit_outcome_metric(self, outcome: TerminalOutcome | None) -> None:
        """Emit the per-outcome lifecycle counter and log."""
        if outcome is None:
            return
        self._metrics.increment(
            f"pipeline.work_unit.{outcome.status.value}",
            tags={"stage": self._stage},
        )
        logger.info(
            "Work-unit reached terminal outcome",
            extra={"stage": self._stage, "outcome": outcome.status.value},
        )

    def _cleanup(self, handle: WorkUnitHandle) -> None:
        """Release a unit's transient resources; never raises."""
        try:
            self._handler.cleanup(handle)
        except Exception:
            logger.exception(
                "Cleanup raised",
                extra={"stage": self._stage, "work_unit_ref": str(handle)},
            )
        self._metrics.increment("pipeline.work_unit.cleaned_up", tags={"stage": self._stage})

    # --- public run surfaces --------------------------------------------------

    def _fill_slots(self, executor: ThreadPoolExecutor) -> bool:
        """Claim and submit eligible units up to the concurrency limit.

        Returns ``True`` if at least one unit was claimed this pass; stops at the
        first ineligible claim or when the concurrency bound is reached.
        """
        claimed = False
        while len(self._slots) < self._handler.concurrency_limit:
            handle = self._claim_next()
            if handle is None:
                break
            self._submit(executor, handle)
            claimed = True
        return claimed

    def run_until_idle(self, *, max_iterations: int = 100_000) -> None:
        """Drive the loop until no work remains in flight and none is eligible.

        Deterministic driver for tests and one-shot batch runs: claims up to the
        concurrency limit, enforces deadlines, reaps finished units, and returns
        once the queue is drained and no slot is occupied. Honors a shutdown
        request by stopping new claims and draining what is in flight.
        """
        self._ensure_validated()
        with ThreadPoolExecutor(max_workers=self._handler.concurrency_limit) as executor:
            for _ in range(max_iterations):
                self._enforce_deadlines()
                self._reap()
                claimed = False
                if not self._shutdown.is_shutdown_requested():
                    claimed = self._fill_slots(executor)
                if not claimed and not self._slots:
                    # No slot occupied and the last fill produced no eligible
                    # unit: the queue is drained.
                    return
                if not claimed:
                    time.sleep(self._poll_interval)
            raise RuntimeError("run_until_idle exceeded max_iterations")

    def run(self) -> int:
        """Run the full supervised loop with signal handling and graceful drain.

        Returns an exit code: ``0`` for a clean drain, ``1`` for a forced exit.
        Installs the shutdown controller's signal handlers, validates
        dependencies, advertises the stage role, then loops claiming and
        processing until a shutdown signal arrives, after which it stops
        claiming and drains in-flight units within the bounded drain timeout.
        """
        self._shutdown.install_signal_handlers()
        self.set_process_title()
        self._ensure_validated()
        logger.info(
            "Starting stage runner loop",
            extra={
                "stage": self._stage,
                "concurrency_limit": self._handler.concurrency_limit,
                "drain_timeout": self._drain_timeout,
            },
        )
        force_terminated = False
        executor = ThreadPoolExecutor(max_workers=self._handler.concurrency_limit)
        try:
            while not self._shutdown.is_shutdown_requested():
                self._maybe_recover_stale()
                self._enforce_deadlines()
                self._reap()
                claimed = self._fill_slots(executor)
                if not claimed:
                    time.sleep(self._poll_interval)
            logger.info(
                "Shutdown requested — draining in-flight work-units",
                extra={"stage": self._stage, "in_flight": len(self._slots)},
            )
            force_terminated = self._drain()
        finally:
            executor.shutdown(wait=False)
            self._shutdown.restore_signal_handlers()
        if self._shutdown.is_immediate_shutdown() or force_terminated:
            logger.warning("Forced shutdown — exit code 1", extra={"stage": self._stage})
            # os._exit bypasses the atexit join of the non-daemon thread pool that
            # would otherwise hang on a stuck/uncooperative in-process unit.
            self._force_exit(1)
            return 1  # reached only when force_exit is patched in tests; os._exit never returns
        logger.info("Shutdown complete — exit code 0", extra={"stage": self._stage})
        return 0

    def _maybe_recover_stale(self) -> None:
        """Run stale recovery if the recovery interval has elapsed."""
        now = self._clock()
        if now - self._last_recovery >= self._stale_recovery_interval:
            self._recover_stale()
            self._last_recovery = now

    def _drain(self) -> bool:
        """Wait for in-flight units to finish within the drain timeout.

        Returns ``True`` if any unit was still running when the timeout elapsed
        (a forced exit). Finalizes everything that finished; on timeout, leaves
        no unit *running* by cancelling and terminating survivors via the
        adapter's cancellation hook before returning.
        """
        with self._slots_lock:
            futures = list(self._slots.keys())
        if not futures:
            return False
        deadline = self._clock() + self._drain_timeout
        while self._clock() < deadline:
            self._enforce_deadlines()
            self._reap()
            with self._slots_lock:
                remaining = list(self._slots.values())
            if not remaining:
                return False
            time.sleep(min(self._poll_interval, 0.05))
        # Drain timeout elapsed — terminate survivors so none is left running.
        with self._slots_lock:
            survivors = list(self._slots.values())
        if not survivors:
            return False
        for slot in survivors:
            slot.cancel_requested = True
            self._request_cancel(slot.handle)
        # Give cancellation a bounded grace, then finalize whatever finished.
        grace_deadline = self._clock() + self._cancel_grace
        while self._clock() < grace_deadline:
            self._reap()
            with self._slots_lock:
                if not self._slots:
                    break
            time.sleep(min(self._poll_interval, 0.05))
        self._reap()
        logger.warning(
            "Drain timeout elapsed; terminated survivors",
            extra={"stage": self._stage},
        )
        return True

    # --- internals ------------------------------------------------------------

    def _ensure_validated(self) -> None:
        """Validate dependencies once before accepting work.

        Enforces the startup gate: if the handler's validation raises, the
        exception propagates and no work is ever claimed.
        """
        if not self._dependencies_validated:
            self.validate_dependencies()


__all__ = ["InMemoryMetrics", "StageRunner", "StageMetrics"]
