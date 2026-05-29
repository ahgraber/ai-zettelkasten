"""Lifecycle fault-injection matrix tests (gate G5).

Gate G5 enumerates a matrix of fault timings at the runtime boundary. This module
holds the genuinely-new combinations; the already-covered cells are cited rather
than duplicated:

- **signal during finalize** — :func:`test_signal_during_finalize_does_not_lose_completed_unit`
- **timeout during/around subprocess cleanup** —
  :func:`test_subprocess_timeout_cleans_up_exactly_once`
- **cancellation during DB contention (finalize lock)** —
  :func:`test_cancel_during_finalize_lock_reaches_cancelled`
- **claim during DB contention** — ``test_runner_finalize_faults::
  test_claim_db_lock_does_not_forget_running_work``
- **stale recovery after interrupted execution** —
  ``test_runner_recovery::test_stale_unit_recovered_with_cause``
- **two runners sharing one process** —
  ``test_runner_two_runner_shutdown::test_one_signal_drains_two_harnesses_in_one_process``

Execution is thread-pool + optional subprocess (not asyncio); act phases wrap
``no_thread_leaks``.
"""

from __future__ import annotations

import datetime as dt
import signal
import threading

from pyleak import no_thread_leaks
import pytest
from sqlalchemy.exc import OperationalError

from aizk.pipeline.runner import StageRunner
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.shutdown import ShutdownController

from ._stub_handler import (
    StubStageHandler,
    SubprocessStubRepository,
    create_stub_engine,
    succeed,
)


def _op_lock() -> OperationalError:
    """Build an ``OperationalError`` resembling a SQLite write-lock contention."""
    return OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))


def test_signal_during_finalize_does_not_lose_completed_unit() -> None:
    """A shutdown signal arriving while finalize is failing does not lose the unit.

    The unit's work succeeds, but the terminal transition first errors once
    (slot kept for retry). A shutdown is requested in that window. The runner
    must still drain — its drain loop re-reaps the kept slot — so the completed
    unit reaches its terminal status (never stranded ``running``) and the runner
    exits cleanly.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=1)
    controller = ShutdownController()

    started = threading.Event()

    def _signal_then_finish(_label: str) -> str:
        # Mark the unit as in flight; the driver requests shutdown while this
        # unit is completing and its first finalize is set up to fail.
        started.set()
        return "ok"

    unit_id = handler.enqueue("completes", behavior=_signal_then_finish)
    # First finalize attempt errors; the runner keeps the slot and retries it,
    # including during the drain that the shutdown signal triggers.
    handler.inject_finalize_fault(times=1, error=_op_lock())

    runner = StageRunner(
        handler,
        engine,
        shutdown=controller,
        poll_interval=0.01,
        drain_timeout=5.0,
    )

    exit_code: list[int] = []

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=lambda: exit_code.append(runner.run()))
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        controller.request_shutdown()  # signal arrives around finalize
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "runner drained despite the finalize fault during shutdown"

    assert exit_code == [0], "runner drained cleanly"
    assert handler.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value, (
        "completed unit reached its terminal status despite signal-during-finalize"
    )
    assert handler.recorded.finalize_attempts.count(str(unit_id)) >= 2, "finalize was retried after the fault"
    assert handler.recorded.cleaned_up.count(str(unit_id)) == 1, "cleanup ran exactly once, on the durable terminal"


# The subprocess-isolated stub uses ``fork`` so its test-only child entrypoint
# (under ``tests/``) is importable in the child; forking from the runner worker
# thread emits a benign DeprecationWarning (the child only ``setpgrp``s + sleeps).
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_subprocess_timeout_cleans_up_exactly_once(tmp_path) -> None:
    """A timed-out subprocess unit cleans up exactly once on the timed-out terminal.

    Sharpens the timeout/subprocess-cleanup cell of the matrix beyond
    ``test_runner_exec::test_timeout_recorded_no_orphans`` (which asserts cleanup
    *ran*): here cleanup must run **exactly once** and the unit must reach the
    timed-out terminal, proving the timeout-around-cleanup path neither double-
    cleans nor leaks the slot.
    """
    engine = create_stub_engine()
    handler = SubprocessStubRepository(
        engine,
        str(tmp_path),
        concurrency_limit=1,
        timeout=dt.timedelta(seconds=0.3),
        stale_after=dt.timedelta(minutes=5),
    )
    unit_id = handler.enqueue("subproc")

    runner = StageRunner(handler, engine, poll_interval=0.02, cancel_grace=3.0)

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=runner.run_until_idle)
        driver.start()
        handler.child_pids(unit_id)  # ensure the child actually spawned before timeout
        driver.join(timeout=20.0)
        assert not driver.is_alive(), "runner drained after the subprocess timeout"

    assert handler.get_status(unit_id) == WorkUnitStatus.TIMED_OUT.value, "subprocess unit recorded timed-out"
    assert handler.recorded.cleaned_up.count(str(unit_id)) == 1, "cleanup ran exactly once on the timed-out terminal"
    assert handler.terminated_signals[0] == signal.SIGTERM, "graceful SIGTERM attempted before any SIGKILL"


def test_cancel_during_finalize_lock_reaches_cancelled() -> None:
    """A cancel whose terminal write hits a DB lock still reaches the cancelled outcome.

    A running unit is cancelled; the runner writes the CANCELLED terminal
    transition, but the first finalize attempt hits a simulated DB lock. The
    runner must keep the slot and retry on the next reap until the CANCELLED
    transition durably lands — the cancelled unit is not stranded ``running`` —
    and cleanup runs exactly once.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=1)

    started = threading.Event()

    def _block_until_cancelled(handle: str) -> str:
        started.set()
        handler.cancel_event(int(handle)).wait(timeout=10.0)
        return "stopped"

    unit_id = handler.enqueue("running", behavior=_block_until_cancelled)
    # The terminal (CANCELLED) finalize first hits a lock, then commits.
    handler.inject_finalize_fault(times=1, error=_op_lock())

    runner = StageRunner(handler, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=runner.run_until_idle)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        runner.cancel_handle(unit_id)
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "runner drained after the contended cancel finalize"

    assert handler.get_status(unit_id) == WorkUnitStatus.CANCELLED.value, (
        "cancelled unit reached its terminal status despite the finalize lock"
    )
    assert handler.recorded.finalize_attempts.count(str(unit_id)) >= 2, "finalize was retried after the lock"
    assert handler.recorded.cleaned_up.count(str(unit_id)) == 1, "cleanup ran exactly once on the durable terminal"
    assert str(unit_id) in handler.recorded.cancelled, "cancel hook was invoked"
