"""Runner bounded-execution tests: wall-clock timeout, no orphans, cleanup.

Covers the spec requirement that a unit exceeding its wall-clock timeout is
terminated and recorded timed-out; that for a subprocess-isolated stage,
termination attempts graceful before forceful and leaves no orphaned descendant
processes; and that transient resources are released on every terminal outcome
(succeeded, failed, cancelled, timed out).

Execution is thread-pool + optional subprocess (not asyncio); act phases wrap
``no_thread_leaks``.
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import threading
import time

from pyleak import no_thread_leaks
import pytest

from aizk.pipeline.runner import StageRunner
from aizk.pipeline.lifecycle import WorkUnitStatus

from ._stub_handler import SubprocessStubRepository, create_stub_engine, succeed


def _pid_alive(pid: int) -> bool:
    """Return ``True`` if a process with ``pid`` still exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    """Poll until ``pid`` is gone or ``timeout`` elapses; return whether it died."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


class _RetryableError(RuntimeError):
    """A retryable failure for the cleanup-on-every-outcome matrix."""

    retryable = True


# The subprocess-isolated stub uses ``fork`` so its test-only child entrypoint
# (under ``tests/``, not importable by a re-execed ``spawn`` interpreter) is
# available in the child; forking from the runner worker thread emits a benign
# DeprecationWarning (the child only ``setpgrp``s and sleeps). Production
# subprocess stages use ``spawn`` (see conversion's worker).
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_timeout_recorded_no_orphans(tmp_path) -> None:
    """A timed-out subprocess unit is terminated gracefully-first with no orphans.

    The subprocess-isolated unit spawns a child that itself spawns a grandchild
    and sleeps past the wall-clock timeout. When the timeout elapses the runner
    terminates the unit; the adapter SIGTERMs the process group before SIGKILL,
    and afterward neither the child nor the grandchild survives. The unit is
    recorded with the timed-out outcome.
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

    def _drive() -> None:
        runner.run_until_idle()

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_drive)
        driver.start()
        # Capture the spawned descendants before they are terminated.
        child_pid, grandchild_pid = handler.child_pids(unit_id)
        driver.join(timeout=20.0)
        assert not driver.is_alive(), "runner drained after the timeout"

    assert handler.get_status(unit_id) == WorkUnitStatus.TIMED_OUT.value, "unit recorded timed-out"
    # Graceful before forceful: SIGTERM precedes any SIGKILL.
    assert handler.terminated_signals, "the process group was signalled"
    assert handler.terminated_signals[0] == signal.SIGTERM, "graceful SIGTERM attempted first"
    # No orphaned descendants remain.
    assert _wait_dead(child_pid), f"child {child_pid} left orphaned"
    assert _wait_dead(grandchild_pid), f"grandchild {grandchild_pid} left orphaned"
    assert str(unit_id) in handler.recorded.cleaned_up, "resources released on timeout"


def test_cleanup_on_every_outcome(tmp_path) -> None:
    """Transient resources are released on succeeded, failed, cancelled, and timed-out.

    Drives one unit into each terminal outcome and asserts the adapter's cleanup
    ran for every one. Uses the in-process stub (cleanup is outcome-independent).
    """
    from ._stub_handler import StubStageHandler

    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=4, timeout=dt.timedelta(seconds=0.3))

    def _fail(_label: str) -> str:
        raise _RetryableError("boom")

    def _sleep_past_timeout(handle: str) -> str:
        # Sleep longer than the wall-clock timeout; honor cooperative cancel so
        # the worker thread is reclaimed rather than leaked.
        handler.cancel_event(int(handle)).wait(timeout=10.0)
        return "stopped"

    succeeded_id = handler.enqueue("succeeded", behavior=succeed)
    failed_id = handler.enqueue("failed", behavior=_fail)
    timed_out_id = handler.enqueue("timed_out", behavior=_sleep_past_timeout)

    # Cancelled: a separate single-slot runner so we can cancel it mid-flight.
    cancel_engine = create_stub_engine()
    cancel_handler = StubStageHandler(cancel_engine, concurrency_limit=1)
    cancel_started = threading.Event()

    def _wait_cancel(handle: str) -> str:
        cancel_started.set()
        cancel_handler.cancel_event(int(handle)).wait(timeout=10.0)
        return "stopped"

    cancelled_id = cancel_handler.enqueue("cancelled", behavior=_wait_cancel)

    runner = StageRunner(handler, engine, poll_interval=0.02, cancel_grace=2.0)
    cancel_runner = StageRunner(cancel_handler, cancel_engine, poll_interval=0.02)

    with no_thread_leaks(action="raise"):
        cancel_driver = threading.Thread(target=cancel_runner.run_until_idle)
        cancel_driver.start()
        assert cancel_started.wait(timeout=5.0)
        cancel_runner.cancel_handle(cancelled_id)

        runner.run_until_idle()
        cancel_driver.join(timeout=10.0)
        assert not cancel_driver.is_alive()

    assert handler.get_status(succeeded_id) == WorkUnitStatus.SUCCEEDED.value
    assert handler.get_status(failed_id) == WorkUnitStatus.FAILED.value
    assert handler.get_status(timed_out_id) == WorkUnitStatus.TIMED_OUT.value
    assert cancel_handler.get_status(cancelled_id) == WorkUnitStatus.CANCELLED.value

    cleaned = set(handler.recorded.cleaned_up)
    assert {str(succeeded_id), str(failed_id), str(timed_out_id)} <= cleaned, "cleanup ran for every in-handler outcome"
    assert str(cancelled_id) in cancel_handler.recorded.cleaned_up, "cleanup ran for the cancelled outcome"
