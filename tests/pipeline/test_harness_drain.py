"""Harness graceful-drain tests.

Covers the spec requirement that on a shutdown signal the harness stops claiming
new work, allows in-flight units to finish within a bounded drain timeout, then
exits — with no work-unit left running afterward — and that the drain timeout is
enforced when in-flight work does not complete in time.

Execution is thread-pool + optional subprocess (not asyncio); act phases wrap
``no_thread_leaks``.
"""

from __future__ import annotations

import datetime as dt
import threading

from pyleak import no_thread_leaks

from aizk.pipeline.harness import StageHarness
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.shutdown import ShutdownController

from ._stub_repository import StubStageRepository, create_stub_engine


def test_inflight_finishes_during_drain() -> None:
    """In-flight work completes during drain; the harness exits cleanly, none running.

    A unit is in flight when shutdown is requested and finishes within the drain
    timeout, so it reaches a terminal outcome and the harness exits with code 0
    and no unit left running.
    """
    engine = create_stub_engine()
    repo = StubStageRepository(engine, concurrency_limit=2)

    started = threading.Event()
    release = threading.Event()

    def _wait_then_finish(_label: str) -> str:
        started.set()
        release.wait(timeout=5.0)
        return "ok"

    unit_id = repo.enqueue("inflight", behavior=_wait_then_finish)

    controller = ShutdownController()
    harness = StageHarness(
        repo,
        engine,
        shutdown=controller,
        poll_interval=0.01,
        drain_timeout=5.0,
    )

    exit_code: list[int] = []

    def _run() -> None:
        exit_code.append(harness.run())

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_run)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        controller.request_shutdown()  # signal arrives with work in flight
        release.set()  # the in-flight unit completes within the drain window
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "harness exited"

    assert exit_code == [0], "clean drain exits with code 0"
    assert repo.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value, "in-flight unit reached a terminal outcome"


def test_drain_timeout_enforced() -> None:
    """In-flight work that does not finish in time is abandoned; none left running.

    A unit blocks past the drain timeout. When the timeout elapses the harness
    stops waiting and exits with a forced (non-zero) code, having cancelled the
    survivor so no unit is left running.
    """
    engine = create_stub_engine()
    # Large per-unit timeout so the wall-clock timeout path does not fire first;
    # the drain timeout is the boundary under test.
    repo = StubStageRepository(engine, concurrency_limit=1, timeout=dt.timedelta(seconds=300))

    started = threading.Event()
    stop = threading.Event()

    def _block_until_cancelled(handle: str) -> str:
        started.set()
        # Honor cooperative cancellation so the worker thread does not leak.
        repo.cancel_event(int(handle)).wait(timeout=10.0)
        stop.set()
        return "stopped"

    unit_id = repo.enqueue("stuck", behavior=_block_until_cancelled)

    controller = ShutdownController()
    forced: list[int] = []
    harness = StageHarness(
        repo,
        engine,
        shutdown=controller,
        poll_interval=0.01,
        drain_timeout=0.2,
        cancel_grace=2.0,
        force_exit=forced.append,  # record forced exit instead of os._exit-ing the test runner
    )

    exit_code: list[int] = []

    def _run() -> None:
        exit_code.append(harness.run())

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_run)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        controller.request_shutdown()
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "harness exited despite the stuck unit"

    assert forced == [1], "drain timeout escalated to a forced exit"
    assert exit_code == [1], "drain-timeout forces a non-zero exit"
    assert str(unit_id) in repo.recorded.cancelled, "survivor was cancelled so none is left running"
    assert stop.is_set(), "cooperative cancellation reached the worker"


def test_uncooperative_inprocess_unit_forces_exit() -> None:
    """An in-process unit that ignores cancellation does not hang the harness.

    The unit blocks on a release the harness cannot trigger (it ignores the
    cooperative cancel), so the drain timeout and cancel grace both elapse with
    it still running. The harness must escalate to a forced exit rather than
    block on the stuck worker — in production ``os._exit`` then kills the leaked
    worker; here the forced-exit hook is recorded so the test asserts the
    escalation without exiting the runner, releasing the worker afterward so the
    test itself leaks no thread.
    """
    engine = create_stub_engine()
    repo = StubStageRepository(engine, concurrency_limit=1, timeout=dt.timedelta(seconds=300))

    started = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def _ignores_cancel(_label: str) -> str:
        started.set()
        # Blocks on a release the harness cannot set; ignores repo.cancel_event.
        release.wait(timeout=10.0)
        done.set()
        return "ignored-cancel"

    repo.enqueue("uncooperative", behavior=_ignores_cancel)

    controller = ShutdownController()
    forced: list[int] = []
    harness = StageHarness(
        repo,
        engine,
        shutdown=controller,
        poll_interval=0.01,
        drain_timeout=0.2,
        cancel_grace=0.2,
        force_exit=forced.append,
    )

    exit_code: list[int] = []

    def _run() -> None:
        exit_code.append(harness.run())

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_run)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        controller.request_shutdown()
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "harness did not block on the uncooperative unit"
        # Release the stuck worker so its pool thread exits cleanly (production
        # force_exit would have killed it), keeping the test free of leaked threads.
        release.set()
        assert done.wait(timeout=5.0), "released worker completed"

    assert forced == [1], "harness escalated to a forced exit"
    assert exit_code == [1], "forced drain returns a non-zero code"
