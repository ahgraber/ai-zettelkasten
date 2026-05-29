"""Runner partial-failure finalization fault-injection tests (gate G4).

Covers the runtime-boundary failure class the spec gates on: completed work must
not be forgotten when the *durable terminal transition* fails. The runner owns
the ``BEGIN IMMEDIATE`` finalize transaction; if it errors, the unit-of-work has
already run but the store still shows ``running``. The contract (F3, runner
``_finalize_slot``) is: keep the concurrency slot, retry ``finalize`` on the next
reap until the terminal transition durably commits, and run ``cleanup`` exactly
once — on the eventual successful commit, never on a failed attempt.

These tests fault-inject two error classes the runner catches in
``_finalize_slot`` — ``OperationalError`` (a simulated DB lock) and the broader
``DBAPIError`` — and assert: the completed unit reaches its terminal status (not
stranded ``running``), the slot was retried (more finalize attempts than commits),
and ``cleanup`` ran exactly once on the durable terminal.

Execution is thread-pool (not asyncio); act phases wrap ``no_thread_leaks``.
"""

from __future__ import annotations

import threading

from pyleak import no_thread_leaks
import pytest
from sqlalchemy.exc import DBAPIError, OperationalError

from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.runner import StageRunner

from ._stub_handler import StubStageHandler, create_stub_engine, succeed


def _op_lock() -> OperationalError:
    """Build an ``OperationalError`` resembling a SQLite write-lock contention."""
    return OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))


def _dbapi_error() -> DBAPIError:
    """Build a generic ``DBAPIError`` resembling a transient DB I/O failure."""
    return DBAPIError("COMMIT", {}, Exception("disk I/O error"))


@pytest.mark.parametrize(
    ("make_error", "fault_count"),
    [
        (_op_lock, 1),
        (_op_lock, 2),
        (_dbapi_error, 1),
    ],
    ids=["operationalerror-once", "operationalerror-twice", "dbapierror-once"],
)
def test_finalize_db_fault_is_retried_until_terminal_lands(make_error, fault_count: int) -> None:
    """A finalize DB fault does not forget the completed unit; cleanup runs once.

    The unit's work succeeds, but the durable terminal transition first errors
    ``fault_count`` times before committing. The runner must keep the slot and
    re-attempt ``finalize`` on subsequent reaps until the transition lands, then
    run ``cleanup`` exactly once. The unit must end ``succeeded`` (never stranded
    ``running``), and there must be strictly more finalize *attempts* than
    *commits* (proving the retry path was taken).
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=1)
    unit_id = handler.enqueue("completes", behavior=succeed)
    handler.inject_finalize_fault(times=fault_count, error=make_error())

    runner = StageRunner(handler, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    assert handler.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value, (
        "completed unit reached its terminal status, not stranded running"
    )
    attempts = handler.recorded.finalize_attempts.count(str(unit_id))
    commits = handler.recorded.finalize_committed.count(str(unit_id))
    assert attempts == fault_count + 1, "finalize was retried once per fault plus the successful attempt"
    assert commits == 1, "exactly one finalize transition durably committed"
    assert handler.recorded.cleaned_up.count(str(unit_id)) == 1, (
        "cleanup ran exactly once — on the durable terminal, not on the failed attempts"
    )


def test_finalize_fault_does_not_clean_up_on_failed_attempt() -> None:
    """Cleanup is withheld while finalize keeps failing, then runs once on success.

    A finalize that fails twice must not release the unit's transient resources
    on either failed attempt: dropping the slot before the durable transition
    landed would strand the unit ``running`` while the runner forgot it. Cleanup
    must run only on the eventual durable terminal.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=1)
    unit_id = handler.enqueue("completes", behavior=succeed)
    handler.inject_finalize_fault(times=2, error=_op_lock())

    runner = StageRunner(handler, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    # The two failed finalize attempts must have produced no cleanup; the third
    # (successful) attempt produces exactly one.
    assert handler.recorded.finalize_attempts.count(str(unit_id)) == 3, "two faults then one success"
    assert handler.recorded.cleaned_up == [str(unit_id)], "cleanup ran exactly once, on the durable terminal"
    assert handler.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value


def test_claim_db_lock_does_not_forget_running_work() -> None:
    """A contended claim lock does not strand or forget already-running work.

    One unit is claimed and held in flight; the *second* claim attempt (while the
    first is still running) hits a simulated DB lock. The runner must tolerate
    the contended claim (skip it that poll, re-poll) and still finalize both
    units — the in-flight one is not forgotten, and the contended one is
    eventually claimed once the injected lock clears.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=2)

    started = threading.Event()
    release = threading.Event()

    def _hold(_label: str) -> str:
        started.set()
        release.wait(timeout=5.0)
        return "ok"

    first_id = handler.enqueue("first", behavior=_hold)
    second_id = handler.enqueue("second", behavior=succeed)
    # Claim call #1 succeeds (claims the held first unit); call #2 — attempted in
    # the same poll while the first is in flight — hits a lock. The runner keeps
    # the running first unit and re-polls, claiming the second once the lock clears.
    handler.inject_claim_fault(on_calls={2}, error=_op_lock())

    runner = StageRunner(handler, engine, poll_interval=0.01)
    exited = threading.Event()

    def _drive() -> None:
        runner.run_until_idle()
        exited.set()

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_drive)
        driver.start()
        assert started.wait(timeout=5.0), "first unit began executing despite the impending claim lock"
        release.set()
        driver.join(timeout=10.0)
        assert not driver.is_alive(), "runner drained after the contended claim cleared"

    assert exited.is_set()
    assert handler.get_status(first_id) == WorkUnitStatus.SUCCEEDED.value, "first unit not forgotten by a claim lock"
    assert handler.get_status(second_id) == WorkUnitStatus.SUCCEEDED.value, "contended unit claimed after lock cleared"
    assert sorted(handler.recorded.cleaned_up) == sorted([str(first_id), str(second_id)]), (
        "both units cleaned up exactly once"
    )
