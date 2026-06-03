"""Runner cancellation tests.

Covers the spec requirement that a cancel request against a running work-unit
takes effect within a bounded interval (the unit stops and reaches a cancelled
outcome), and that a work-unit cancelled while still queued is skipped rather
than executed.

Execution is thread-pool + optional subprocess (not asyncio); act phases wrap
``no_thread_leaks``.
"""

from __future__ import annotations

import threading
import time

from pyleak import no_thread_leaks

from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.runner import StageRunner

from ._stub_handler import StubStageHandler, create_stub_engine


def test_running_cancelled_promptly() -> None:
    """A running unit is cancelled promptly and reaches the cancelled outcome.

    The unit blocks on its cooperative-cancel event. Once running, the test asks
    the runner to cancel it; the adapter's cancel hook releases the worker, and
    the runner finalizes the unit as cancelled within a bounded interval.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=1)

    started = threading.Event()

    def _block_until_cancelled(handle: str) -> str:
        started.set()
        handler.cancel_event(int(handle)).wait(timeout=10.0)
        return "stopped"

    unit_id = handler.enqueue("running", behavior=_block_until_cancelled)

    runner = StageRunner(handler, engine, poll_interval=0.01)

    def _drive() -> None:
        runner.run_until_idle()

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_drive)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"

        cancel_observed = time.monotonic()
        runner.cancel_handle(unit_id)
        driver.join(timeout=10.0)
        elapsed = time.monotonic() - cancel_observed
        assert not driver.is_alive(), "runner drained after cancellation"

    assert elapsed < 5.0, f"cancellation took {elapsed:.2f}s — not within a bounded interval"
    assert str(unit_id) in handler.recorded.cancelled, "cancel hook was invoked"
    assert handler.get_status(unit_id) == WorkUnitStatus.CANCELLED.value, "unit reached the cancelled outcome"


def test_queued_cancel_skipped() -> None:
    """A unit cancelled while queued is skipped and never executed.

    A unit seeded in a cancelled state is not eligible, so the runner drains
    without ever calling ``execute`` for it.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=2)

    cancelled_id = handler.enqueue("cancelled-while-queued", status=WorkUnitStatus.CANCELLED.value)
    runnable_id = handler.enqueue("runnable")

    runner = StageRunner(handler, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    assert str(cancelled_id) not in handler.recorded.execute_started, "queued-cancelled unit was never executed"
    assert handler.get_status(cancelled_id) == WorkUnitStatus.CANCELLED.value, "it stays cancelled"
    assert handler.get_status(runnable_id) == WorkUnitStatus.SUCCEEDED.value, "the runnable unit still ran"
