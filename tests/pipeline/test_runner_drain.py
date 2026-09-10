"""Runner graceful-drain tests.

Covers the spec requirement that on a shutdown signal the runner stops claiming
new work, allows in-flight units to finish within a bounded drain timeout, then
exits — with no work-unit left running afterward — that the drain timeout is
enforced when in-flight work does not complete in time, and that a unit the
drain timeout terminated is a retryable failure the next runtime claims.

Execution is thread-pool + optional subprocess (not asyncio); act phases wrap
``no_thread_leaks``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import threading

from pyleak import no_thread_leaks
from sqlmodel import Session

from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.runner import StageRunner
from aizk.pipeline.shutdown import ShutdownController

from ._stub_handler import StubStageHandler, StubWorkUnit, create_stub_engine, create_stub_file_engine, succeed


def test_inflight_finishes_during_drain() -> None:
    """In-flight work completes during drain; the runner exits cleanly, none running.

    A unit is in flight when shutdown is requested and finishes within the drain
    timeout, so it reaches a terminal outcome and the runner exits with code 0
    and no unit left running.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=2)

    started = threading.Event()
    release = threading.Event()

    def _wait_then_finish(_label: str) -> str:
        started.set()
        release.wait(timeout=5.0)
        return "ok"

    unit_id = handler.enqueue("inflight", behavior=_wait_then_finish)

    controller = ShutdownController()
    runner = StageRunner(
        handler,
        engine,
        shutdown=controller,
        poll_interval=0.01,
        drain_timeout=5.0,
    )

    exit_code: list[int] = []

    def _run() -> None:
        exit_code.append(runner.run())

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_run)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        controller.request_shutdown()  # signal arrives with work in flight
        release.set()  # the in-flight unit completes within the drain window
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "runner exited"

    assert exit_code == [0], "clean drain exits with code 0"
    assert handler.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value, "in-flight unit reached a terminal outcome"


def test_drain_timeout_enforced() -> None:
    """In-flight work that does not finish in time is abandoned; none left running.

    A unit blocks past the drain timeout. When the timeout elapses the runner
    stops waiting and exits with a forced (non-zero) code, having terminated
    the survivor so no unit is left running; the survivor is recorded as a
    retryable failure naming the drain, not as cancelled.
    """
    engine = create_stub_engine()
    # Large per-unit timeout so the wall-clock timeout path does not fire first;
    # the drain timeout is the boundary under test.
    handler = StubStageHandler(engine, concurrency_limit=1, timeout=dt.timedelta(seconds=300))

    started = threading.Event()
    stop = threading.Event()

    def _block_until_cancelled(handle: str) -> str:
        started.set()
        # Honor cooperative cancellation so the worker thread does not leak.
        handler.cancel_event(int(handle)).wait(timeout=10.0)
        stop.set()
        return "stopped"

    unit_id = handler.enqueue("stuck", behavior=_block_until_cancelled)

    controller = ShutdownController()
    forced: list[int] = []
    runner = StageRunner(
        handler,
        engine,
        shutdown=controller,
        poll_interval=0.01,
        drain_timeout=0.2,
        cancel_grace=2.0,
        force_exit=forced.append,  # record forced exit instead of os._exit-ing the test runner
    )

    exit_code: list[int] = []

    def _run() -> None:
        exit_code.append(runner.run())

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_run)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        controller.request_shutdown()
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "runner exited despite the stuck unit"

    assert forced == [1], "drain timeout escalated to a forced exit"
    assert exit_code == [1], "drain-timeout forces a non-zero exit"
    assert str(unit_id) in handler.recorded.cancelled, "survivor was cancelled so none is left running"
    assert stop.is_set(), "cooperative cancellation reached the worker"
    with Session(engine) as session:
        unit = session.get(StubWorkUnit, unit_id)
        assert unit is not None
        assert unit.status is WorkUnitStatus.FAILED, "a drain-terminated unit is a failure, not a cancel"
        assert unit.earliest_next_attempt_at is not None, "the failure carries a retry wait"
        assert unit.error_code == "worker_drain_terminated", "the error code names the drain"


def test_drain_terminated_unit_is_claimed_by_the_next_runner(tmp_path: Path) -> None:
    """A unit the drain timeout terminated is retried by the next runtime without operator action.

    The first runner drains with a unit still in flight and terminates it as a
    retryable failure. A second runner over the same file-backed store then
    claims that unit and runs it to success.
    """
    db_path = str(tmp_path / "stub-drain.db")
    engine_a = create_stub_file_engine(db_path, create_schema=True)
    handler_a = StubStageHandler(engine_a, concurrency_limit=1, timeout=dt.timedelta(seconds=300))

    started = threading.Event()

    def _block_until_cancelled(handle: str) -> str:
        started.set()
        handler_a.cancel_event(int(handle)).wait(timeout=10.0)
        return "stopped"

    unit_id = handler_a.enqueue("interrupted", behavior=_block_until_cancelled)
    controller = ShutdownController()
    runner_a = StageRunner(
        handler_a,
        engine_a,
        shutdown=controller,
        poll_interval=0.01,
        drain_timeout=0.2,
        cancel_grace=2.0,
        force_exit=lambda _code: None,
    )

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=runner_a.run)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        controller.request_shutdown()
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "first runner exited"
    engine_a.dispose()

    with Session(create_stub_file_engine(db_path, create_schema=False)) as session:
        interrupted = session.get(StubWorkUnit, unit_id)
        assert interrupted is not None
        assert interrupted.status is WorkUnitStatus.FAILED
        assert interrupted.earliest_next_attempt_at is not None
        assert interrupted.error_code == "worker_drain_terminated"

    engine_b = create_stub_file_engine(db_path, create_schema=False)
    handler_b = StubStageHandler(engine_b, concurrency_limit=1)
    handler_b.register_behavior(unit_id, succeed)
    runner_b = StageRunner(handler_b, engine_b, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner_b.run_until_idle()

    assert handler_b.recorded.execute_started == [str(unit_id)], "the next runner claimed the interrupted unit"
    assert handler_b.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value
    engine_b.dispose()


def test_drain_terminated_unit_at_the_cap_is_permanent(tmp_path: Path) -> None:
    """A unit drain-terminated on its last allowed attempt is permanent, and the next runtime leaves it."""
    db_path = str(tmp_path / "stub-drain-cap.db")
    engine_a = create_stub_file_engine(db_path, create_schema=True)
    handler_a = StubStageHandler(
        engine_a, concurrency_limit=1, timeout=dt.timedelta(seconds=300), retry_max_attempts=3
    )

    started = threading.Event()

    def _block_until_cancelled(handle: str) -> str:
        started.set()
        handler_a.cancel_event(int(handle)).wait(timeout=10.0)
        return "stopped"

    unit_id = handler_a.enqueue("last-attempt", behavior=_block_until_cancelled)
    with Session(engine_a) as session:
        unit = session.get(StubWorkUnit, unit_id)
        assert unit is not None
        unit.attempts = 2  # the claim below makes this attempt 3, the cap
        session.add(unit)
        session.commit()
    controller = ShutdownController()
    runner_a = StageRunner(
        handler_a,
        engine_a,
        shutdown=controller,
        poll_interval=0.01,
        drain_timeout=0.2,
        cancel_grace=2.0,
        force_exit=lambda _code: None,
    )

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=runner_a.run)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        controller.request_shutdown()
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "first runner exited"
    engine_a.dispose()

    engine_b = create_stub_file_engine(db_path, create_schema=False)
    handler_b = StubStageHandler(engine_b, concurrency_limit=1, retry_max_attempts=3)
    handler_b.register_behavior(unit_id, succeed)
    runner_b = StageRunner(handler_b, engine_b, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner_b.run_until_idle()

    assert handler_b.recorded.execute_started == [], "a permanent failure is not claimed"
    with Session(engine_b) as session:
        unit = session.get(StubWorkUnit, unit_id)
        assert unit is not None
        assert unit.status is WorkUnitStatus.FAILED
        assert unit.attempts == 3
        assert unit.earliest_next_attempt_at is None, "at the cap the drain failure carries no wait"
        assert unit.finished_at is not None
        assert unit.error_code == "worker_drain_terminated"
    engine_b.dispose()


def test_uncooperative_inprocess_unit_forces_exit() -> None:
    """An in-process unit that ignores cancellation does not hang the runner, and its row is classified.

    The unit blocks on a release the runner cannot trigger (it ignores the
    cooperative cancel), so the drain timeout and cancel grace both elapse with
    it still running. The runner must escalate to a forced exit rather than
    block on the stuck worker — in production ``os._exit`` then kills the leaked
    worker; here the forced-exit hook is recorded so the test asserts the
    escalation without exiting the runner, releasing the worker afterward so the
    test itself leaks no thread. Before the exit the row is recorded as a
    drain failure so it is not left ``running``.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=1, timeout=dt.timedelta(seconds=300))

    started = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def _ignores_cancel(_label: str) -> str:
        started.set()
        # Blocks on a release the runner cannot set; ignores handler.cancel_event.
        release.wait(timeout=10.0)
        done.set()
        return "ignored-cancel"

    unit_id = handler.enqueue("uncooperative", behavior=_ignores_cancel)

    controller = ShutdownController()
    forced: list[int] = []
    runner = StageRunner(
        handler,
        engine,
        shutdown=controller,
        poll_interval=0.01,
        drain_timeout=0.2,
        cancel_grace=0.2,
        force_exit=forced.append,
    )

    exit_code: list[int] = []

    def _run() -> None:
        exit_code.append(runner.run())

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_run)
        driver.start()
        assert started.wait(timeout=5.0), "unit began executing"
        controller.request_shutdown()
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "runner did not block on the uncooperative unit"
        # Release the stuck worker so its pool thread exits cleanly (production
        # force_exit would have killed it), keeping the test free of leaked threads.
        release.set()
        assert done.wait(timeout=5.0), "released worker completed"

    assert forced == [1], "runner escalated to a forced exit"
    assert exit_code == [1], "forced drain returns a non-zero code"
    with Session(engine) as session:
        unit = session.get(StubWorkUnit, unit_id)
        assert unit is not None
        assert unit.status is WorkUnitStatus.FAILED, "the stuck unit's row was classified before the exit"
        assert unit.error_code == "worker_drain_terminated"
        assert unit.earliest_next_attempt_at is not None, "below the cap the next runtime picks it up"
