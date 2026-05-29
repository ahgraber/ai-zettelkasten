"""Runner scheduling tests: bounded concurrency, submission order, retry-wait.

Covers the spec requirement that eligible work-units (queued and past any
retry-wait) are processed without exceeding the configured concurrency limit,
that the earliest-submitted units are the first claimed (claim/selection order,
not serialized thread starts), and that a retryable-failed unit whose retry-wait
has not elapsed is not started.

The execution model is thread-pool + optional subprocess (not asyncio), so the
act phases are wrapped with ``no_thread_leaks`` per design decision
``ExecutionModelIsThreadPlusOptionalSubprocess``.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

from pyleak import no_thread_leaks

from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.runner import StageRunner

from ._stub_handler import StubStageHandler, create_stub_engine


def test_concurrency_within_limit_in_order() -> None:
    """Concurrency never exceeds the limit and eligible units start in order.

    Each unit records its start order and blocks until released, while a live
    counter tracks simultaneous executions. With more eligible units than the
    limit, the observed peak concurrency equals — and never exceeds — the limit,
    and the first units claimed are the earliest submitted.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=2, timeout=dt.timedelta(seconds=30))

    limit = 2
    total = 6
    lock = threading.Lock()
    live = 0
    peak = 0
    start_order: list[str] = []
    release = threading.Event()

    def _blocking(label: str) -> str:
        nonlocal live, peak
        with lock:
            start_order.append(label)
            live += 1
            peak = max(peak, live)
        # Hold the slot until every slot is occupied so peak is observable,
        # then release once the test signals.
        release.wait(timeout=5.0)
        with lock:
            live -= 1
        return f"ok:{label}"

    # `behavior` receives the work-unit handle (its id) as a string; record by id.
    ids = [handler.enqueue(f"u{i}", behavior=_blocking) for i in range(total)]
    expected_first = [str(i) for i in ids[:limit]]

    runner = StageRunner(handler, engine, poll_interval=0.01)

    def _drive() -> None:
        runner.run_until_idle()

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_drive)
        driver.start()
        # Wait until the limit's worth of slots are occupied.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with lock:
                if live >= limit:
                    break
            time.sleep(0.01)
        release.set()
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "runner drained"

    assert peak <= limit, f"peak concurrency {peak} exceeded limit {limit}"
    assert peak == limit, "concurrency reached the limit with surplus eligible units"
    # Claim/selection order is the durable guarantee: the first `limit` units to
    # begin are exactly the first `limit` submitted. Their micro start-order within
    # the concurrent batch is scheduler-dependent, so assert membership, not sequence.
    assert set(start_order[:limit]) == set(expected_first), (
        f"first batch was not the earliest-submitted: {start_order}"
    )
    for unit_id in ids:
        assert handler.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value


def test_retry_wait_gates_eligibility() -> None:
    """A retryable-failed unit waiting on its retry-wait is not started.

    A unit seeded as failed-retryable with a future ``earliest_next_attempt_at``
    is not eligible: the runner drains without ever executing it. A second unit
    whose retry-wait has elapsed is eligible and runs.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=2)

    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    waiting_id = handler.enqueue(
        "waiting",
        status=WorkUnitStatus.FAILED.value,
        earliest_next_attempt_at=future,
    )
    ready_id = handler.enqueue(
        "ready",
        status=WorkUnitStatus.FAILED.value,
        earliest_next_attempt_at=past,
    )

    runner = StageRunner(handler, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    assert str(waiting_id) not in handler.recorded.execute_started, "retry-waiting unit was not started"
    assert str(ready_id) in handler.recorded.execute_started, "elapsed-retry-wait unit was started"
    assert handler.get_status(ready_id) == WorkUnitStatus.SUCCEEDED.value
    assert handler.get_status(waiting_id) == WorkUnitStatus.FAILED.value, "waiting unit untouched"


def test_permanent_failure_is_not_reclaimed() -> None:
    """A permanently-failed unit is not re-claimed; only retryable failures retry.

    The stub maps an exception carrying ``retryable=False`` to a permanent
    terminal outcome. The runner must not re-claim it (the eligibility query
    excludes permanent failures), so the drain terminates and the unit ran once
    — exercising the spec rule that only a retryable-failed unit is retry-eligible.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=1)

    class _PermanentError(RuntimeError):
        retryable = False

    def _permfail(_label: str) -> str:
        raise _PermanentError("permanent failure")

    unit_id = handler.enqueue("perm", behavior=_permfail)

    runner = StageRunner(handler, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    assert handler.recorded.execute_started.count(str(unit_id)) == 1, "permanent failure was re-claimed"
    assert handler.get_status(unit_id) == WorkUnitStatus.FAILED.value, "permanent failure ended failed"
