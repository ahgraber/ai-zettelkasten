"""Tests for the work-unit repository's lifecycle rules over the stub stage.

Covers the runtime spec's claim eligibility and attempt counting, the terminal
transition with backoff and the attempt cap, stale recovery and its bound, the
finalize guards against late and lost outcomes, and the operator retry and
cancel transitions. Every timestamp comes from a controllable clock so waits
are asserted exactly.
"""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any

from pyleak import no_thread_leaks
import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.pipeline.repository import IneligibleTransitionError, WorkUnitRepository
from aizk.pipeline.runner import StageRunner
from aizk.pipeline.stage import require_stale_threshold
from aizk.utilities.errors import StartupValidationError

from ._stub_handler import (
    StubEventKind,
    StubStageHandler,
    StubSucceededPayload,
    StubWorkUnit,
    create_stub_file_engine,
)

_NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.timezone.utc)
_CAP = 3
_BASE_DELAY = 10.0
_STALE_AFTER = dt.timedelta(minutes=5)
_ERROR = ("stub_error", "the behavior raised")
_RETRYABLE = TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE)
_PERMANENT = TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.PERMANENT)


class _Clock:
    """A settable clock the repository reads every timestamp from."""

    def __init__(self, now: dt.datetime = _NOW) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += dt.timedelta(**delta)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_stub_file_engine(str(tmp_path / "repository.db"), create_schema=True)
    yield eng
    eng.dispose()


@pytest.fixture
def handler(engine: Engine, clock: _Clock) -> StubStageHandler:
    return StubStageHandler(
        engine,
        clock=clock,
        stale_after=_STALE_AFTER,
        retry_base_delay_seconds=_BASE_DELAY,
        retry_max_attempts=_CAP,
    )


@pytest.fixture
def repository(handler: StubStageHandler) -> WorkUnitRepository[StubWorkUnit]:
    return handler.repository


def _row(engine: Engine, unit_id: int) -> StubWorkUnit:
    with Session(engine) as session:
        row = session.get(StubWorkUnit, unit_id)
        assert row is not None
        session.expunge(row)
        return row


def _set(engine: Engine, unit_id: int, **fields: Any) -> None:
    """Write ``fields`` straight onto the row, seeding durable state for a scenario."""
    with Session(engine) as session:
        row = session.get(StubWorkUnit, unit_id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        session.add(row)
        session.commit()


def _events(engine: Engine, unit_id: int) -> list[PipelineEvent]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(PipelineEvent)
                .where(PipelineEvent.work_unit_ref == str(unit_id))
                .order_by(PipelineEvent.event_id)
            ).all()
        )


def _kinds(engine: Engine, unit_id: int) -> list[str]:
    return [event.kind for event in _events(engine, unit_id)]


def _claim(engine: Engine, repository: WorkUnitRepository[StubWorkUnit]) -> int | None:
    with Session(engine) as session:
        row = repository.claim_next(session, worker_pid=1)
        unit_id = row.id if row is not None else None
        session.commit()
    return unit_id


def _finalize(
    engine: Engine,
    repository: WorkUnitRepository[StubWorkUnit],
    unit_id: int,
    outcome: TerminalOutcome,
    *,
    attempt: int | None = None,
) -> bool:
    with Session(engine) as session:
        written = repository.finalize(
            session,
            unit_id,
            attempt,
            outcome,
            error=_ERROR,
            succeeded=(StubEventKind.SUCCEEDED, StubSucceededPayload(), None),
        )
        session.commit()
    return written


def _recover(engine: Engine, repository: WorkUnitRepository[StubWorkUnit]) -> list[int]:
    with Session(engine) as session:
        recovered = [row.id for row in repository.recover_stale(session)]
        session.commit()
    return recovered


def _transition(
    engine: Engine,
    unit_id: int,
    action: Callable[[Session, StubWorkUnit], None],
) -> None:
    with Session(engine) as session:
        row = session.get(StubWorkUnit, unit_id)
        assert row is not None
        action(session, row)
        session.commit()


def _snapshot(engine: Engine, unit_id: int) -> tuple:
    row = _row(engine, unit_id)
    return (
        row.status,
        row.attempts,
        row.error_code,
        row.error_message,
        row.earliest_next_attempt_at,
        row.last_error_at,
        row.finished_at,
        row.queued_at,
    )


# --- claim eligibility and attempt counting ------------------------------------------


def test_claim_order_follows_submission(engine: Engine, handler: StubStageHandler, repository) -> None:
    """Three queued units are claimed earliest-submitted first, each on attempt 1; then nothing."""
    ids = [handler.enqueue(f"u{i}") for i in range(3)]

    claimed = [_claim(engine, repository) for _ in range(4)]

    assert claimed == [*ids, None]
    assert [_row(engine, unit_id).attempts for unit_id in ids] == [1, 1, 1]
    assert all(_row(engine, unit_id).status is WorkUnitStatus.RUNNING for unit_id in ids)


def test_retryable_failure_below_the_cap_sets_the_wait(engine: Engine, handler: StubStageHandler, repository) -> None:
    """A retryable failure on attempt 1 waits ``base * 2**1`` and is not finished."""
    unit_id = handler.enqueue("flaky")
    _claim(engine, repository)

    assert _finalize(engine, repository, unit_id, _RETRYABLE) is True

    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.FAILED
    assert row.earliest_next_attempt_at == _NOW + dt.timedelta(seconds=_BASE_DELAY * 2)
    assert row.finished_at is None
    assert (row.error_code, row.error_message) == _ERROR
    assert row.last_error_at == _NOW
    failed = _events(engine, unit_id)[-1]
    assert failed.kind == "failed"
    assert json.loads(failed.payload_json)["retryable"] is True


def test_retryable_failure_at_the_cap_is_permanent(engine: Engine, handler: StubStageHandler, repository) -> None:
    """A retryable failure on the last allowed attempt has no wait and a finished time."""
    unit_id = handler.enqueue("exhausted")
    _set(engine, unit_id, attempts=_CAP - 1)
    _claim(engine, repository)
    assert _row(engine, unit_id).attempts == _CAP

    _finalize(engine, repository, unit_id, _RETRYABLE)

    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.FAILED
    assert row.earliest_next_attempt_at is None
    assert row.finished_at == _NOW
    assert json.loads(_events(engine, unit_id)[-1].payload_json)["retryable"] is False


def test_permanent_failure_has_no_wait(engine: Engine, handler: StubStageHandler, repository) -> None:
    """A permanent failure is finished with no retry wait, whatever the attempt."""
    unit_id = handler.enqueue("broken")
    _claim(engine, repository)

    _finalize(engine, repository, unit_id, _PERMANENT)

    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.FAILED
    assert row.earliest_next_attempt_at is None
    assert row.finished_at == _NOW
    assert json.loads(_events(engine, unit_id)[-1].payload_json)["retryable"] is False


def test_permanent_failure_is_never_claimed(
    engine: Engine, handler: StubStageHandler, repository, clock: _Clock
) -> None:
    """A failed unit with no wait is not eligible however much time passes."""
    unit_id = handler.enqueue("broken")
    _claim(engine, repository)
    _finalize(engine, repository, unit_id, _PERMANENT)

    clock.advance(days=1)

    assert _claim(engine, repository) is None
    assert _row(engine, unit_id).attempts == 1


def test_unit_waiting_on_its_retry_delay_is_not_claimed(
    engine: Engine, handler: StubStageHandler, repository, clock: _Clock
) -> None:
    """A retryable failure is claimed only once its wait has elapsed."""
    unit_id = handler.enqueue("flaky")
    _claim(engine, repository)
    _finalize(engine, repository, unit_id, _RETRYABLE)

    assert _claim(engine, repository) is None, "the wait has not elapsed"
    clock.advance(seconds=_BASE_DELAY * 2)

    assert _claim(engine, repository) == unit_id
    assert _row(engine, unit_id).attempts == 2


def test_claim_is_the_only_transition_that_changes_attempts(
    engine: Engine, handler: StubStageHandler, repository, clock: _Clock
) -> None:
    """Failure, stale recovery, operator retry, and cancel leave ``attempts`` alone; each claim adds one."""
    unit_id = handler.enqueue("counted")
    observed: list[int] = []

    _claim(engine, repository)
    observed.append(_row(engine, unit_id).attempts)
    _finalize(engine, repository, unit_id, _RETRYABLE)
    observed.append(_row(engine, unit_id).attempts)
    clock.advance(seconds=_BASE_DELAY * 2)
    _claim(engine, repository)
    observed.append(_row(engine, unit_id).attempts)
    _set(engine, unit_id, started_at=clock.now - _STALE_AFTER - dt.timedelta(seconds=1))
    _recover(engine, repository)
    observed.append(_row(engine, unit_id).attempts)
    _transition(engine, unit_id, lambda s, row: repository.requeue(s, row, reason="operator_retry", principal="op"))
    observed.append(_row(engine, unit_id).attempts)
    _transition(engine, unit_id, lambda s, row: repository.cancel(s, row))
    observed.append(_row(engine, unit_id).attempts)

    assert observed == [1, 1, 2, 2, 2, 2]


def test_two_runners_claim_each_unit_exactly_once(tmp_path: Path) -> None:
    """Two runners over one store contend for three units; each unit is claimed and finished once."""
    db_path = str(tmp_path / "shared.db")
    engine_a = create_stub_file_engine(db_path, create_schema=True)
    engine_b = create_stub_file_engine(db_path, create_schema=False)

    def _slow(label: str) -> str:
        time.sleep(0.05)
        return f"ok:{label}"

    handler_a = StubStageHandler(engine_a, concurrency_limit=1)
    handler_b = StubStageHandler(engine_b, concurrency_limit=1)
    ids = [handler_a.enqueue(f"u{i}", behavior=_slow) for i in range(3)]
    for unit_id in ids:
        handler_b.register_behavior(unit_id, _slow)
    runner_a = StageRunner(handler_a, engine_a, poll_interval=0.01)
    runner_b = StageRunner(handler_b, engine_b, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        drivers = [threading.Thread(target=runner.run_until_idle) for runner in (runner_a, runner_b)]
        for driver in drivers:
            driver.start()
        for driver in drivers:
            driver.join(timeout=20.0)
            assert not driver.is_alive(), "runner drained"

    for unit_id in ids:
        kinds = _kinds(engine_a, unit_id)
        assert kinds.count("claimed") == 1, f"unit {unit_id} was claimed once: {kinds}"
        assert kinds.count("succeeded") == 1, f"unit {unit_id} finished once: {kinds}"
        assert _row(engine_a, unit_id).attempts == 1
    started = sorted(handler_a.recorded.execute_started + handler_b.recorded.execute_started)
    assert started == sorted(str(unit_id) for unit_id in ids), "every unit ran on exactly one runner"
    engine_a.dispose()
    engine_b.dispose()


# --- finalize guards -----------------------------------------------------------------


def test_finalize_after_an_operator_cancel_leaves_cancelled_without_a_second_event(
    engine: Engine, handler: StubStageHandler, repository
) -> None:
    """An operator cancel committed mid-run wins over every late outcome, and no second event is written."""
    unit_id = handler.enqueue("cancelled-mid-run")
    _claim(engine, repository)
    _transition(engine, unit_id, lambda s, row: repository.cancel(s, row))
    kinds_after_cancel = _kinds(engine, unit_id)

    late_outcomes = [TerminalOutcome(WorkUnitStatus.CANCELLED), _RETRYABLE, TerminalOutcome(WorkUnitStatus.SUCCEEDED)]
    written = [_finalize(engine, repository, unit_id, outcome, attempt=1) for outcome in late_outcomes]

    assert written == [False, False, False]
    assert _row(engine, unit_id).status is WorkUnitStatus.CANCELLED
    assert _kinds(engine, unit_id) == kinds_after_cancel
    assert kinds_after_cancel.count("cancelled") == 1


def test_finalize_with_a_stale_attempt_token_is_a_no_op(
    engine: Engine, handler: StubStageHandler, repository, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """An outcome from an execution stale recovery reclaimed does not touch the newer attempt."""
    unit_id = handler.enqueue("reclaimed")
    _claim(engine, repository)  # attempt 1
    _set(engine, unit_id, started_at=clock.now - _STALE_AFTER - dt.timedelta(seconds=1))
    _recover(engine, repository)
    _claim(engine, repository)  # attempt 2 owns the unit now
    before = _snapshot(engine, unit_id)

    with caplog.at_level(logging.WARNING, logger="aizk.pipeline.repository"):
        written = _finalize(engine, repository, unit_id, TerminalOutcome(WorkUnitStatus.SUCCEEDED), attempt=1)

    assert written is False
    assert _snapshot(engine, unit_id) == before
    assert _row(engine, unit_id).status is WorkUnitStatus.RUNNING
    assert "succeeded" not in _kinds(engine, unit_id)
    assert any("Finalize skipped" in record.message for record in caplog.records)


def test_finalize_after_another_process_recovered_the_unit_writes_nothing(tmp_path: Path, clock: _Clock) -> None:
    """A stale sweep elsewhere leaves the token intact but the row ``failed``; the old finalize is refused."""
    db_path = str(tmp_path / "recovered.db")
    engine_a = create_stub_file_engine(db_path, create_schema=True)
    engine_b = create_stub_file_engine(db_path, create_schema=False)
    handler_a = StubStageHandler(engine_a, clock=clock, stale_after=_STALE_AFTER, retry_max_attempts=_CAP)
    handler_b = StubStageHandler(engine_b, clock=clock, stale_after=_STALE_AFTER, retry_max_attempts=_CAP)
    unit_id = handler_a.enqueue("recovered-elsewhere")
    assert _claim(engine_a, handler_a.repository) == unit_id
    _set(engine_a, unit_id, started_at=clock.now - _STALE_AFTER - dt.timedelta(seconds=1))
    assert _recover(engine_b, handler_b.repository) == [unit_id]
    before = (_snapshot(engine_a, unit_id), _kinds(engine_a, unit_id))
    assert before[0][:2] == (WorkUnitStatus.FAILED, 1)

    written = _finalize(engine_a, handler_a.repository, unit_id, TerminalOutcome(WorkUnitStatus.SUCCEEDED), attempt=1)

    assert written is False
    assert (_snapshot(engine_a, unit_id), _kinds(engine_a, unit_id)) == before
    engine_a.dispose()
    engine_b.dispose()


def test_finalize_after_an_operator_cancel_and_retry_writes_nothing(
    engine: Engine, handler: StubStageHandler, repository
) -> None:
    """A cancel then a retry leave the row ``queued`` on the same attempt; the old finalize is refused."""
    unit_id = handler.enqueue("cancelled-then-retried")
    _claim(engine, repository)
    _transition(engine, unit_id, lambda s, row: repository.cancel(s, row))
    _transition(engine, unit_id, lambda s, row: repository.requeue(s, row, reason="operator_retry", principal="op"))
    before = (_snapshot(engine, unit_id), _kinds(engine, unit_id))
    assert before[0][:2] == (WorkUnitStatus.QUEUED, 1)

    written = [
        _finalize(engine, repository, unit_id, outcome, attempt=1)
        for outcome in (
            TerminalOutcome(WorkUnitStatus.SUCCEEDED),
            _RETRYABLE,
            TerminalOutcome(WorkUnitStatus.TIMED_OUT),
        )
    ]

    assert written == [False, False, False]
    assert (_snapshot(engine, unit_id), _kinds(engine, unit_id)) == before


def test_finalize_repeated_with_the_same_token_writes_no_second_event(
    engine: Engine, handler: StubStageHandler, repository
) -> None:
    """A finalize retried after its commit already landed is refused, so the trail holds one terminal event."""
    unit_id = handler.enqueue("committed-once")
    _claim(engine, repository)
    assert _finalize(engine, repository, unit_id, _RETRYABLE, attempt=1) is True
    before = (_snapshot(engine, unit_id), _kinds(engine, unit_id))

    written = _finalize(engine, repository, unit_id, _RETRYABLE, attempt=1)

    assert written is False
    assert (_snapshot(engine, unit_id), _kinds(engine, unit_id)) == before
    assert _kinds(engine, unit_id).count("failed") == 1


def test_units_enqueued_at_the_same_instant_are_claimed_in_id_order(
    engine: Engine, handler: StubStageHandler, repository
) -> None:
    """Under one fixed clock two units share ``queued_at``; the lower id is claimed first."""
    first = handler.enqueue("first")
    second = handler.enqueue("second")
    assert _row(engine, first).queued_at == _row(engine, second).queued_at == _NOW

    assert [_claim(engine, repository) for _ in range(2)] == [first, second]


def test_finalize_timed_out_on_a_succeeded_row_is_a_no_op(
    engine: Engine, handler: StubStageHandler, repository
) -> None:
    """A late timeout never overwrites a unit that already succeeded."""
    unit_id = handler.enqueue("done")
    _claim(engine, repository)
    _finalize(engine, repository, unit_id, TerminalOutcome(WorkUnitStatus.SUCCEEDED))

    written = _finalize(engine, repository, unit_id, TerminalOutcome(WorkUnitStatus.TIMED_OUT))

    assert written is False
    assert _row(engine, unit_id).status is WorkUnitStatus.SUCCEEDED
    assert _kinds(engine, unit_id) == ["queued", "claimed", "succeeded"]


# --- stale recovery ------------------------------------------------------------------


def test_stale_recovery_below_the_cap_sets_an_immediate_wait(
    engine: Engine, handler: StubStageHandler, repository
) -> None:
    """A stranded unit below the cap is failed with an immediate wait and a ``recovered_stale`` event."""
    unit_id = handler.enqueue("stranded")
    _claim(engine, repository)
    stranded_at = _NOW - _STALE_AFTER - dt.timedelta(seconds=1)
    _set(engine, unit_id, started_at=stranded_at)

    assert _recover(engine, repository) == [unit_id]

    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.FAILED
    assert row.earliest_next_attempt_at == _NOW
    assert row.error_code == "worker_stale_running"
    assert row.finished_at is None
    assert row.attempts == 1
    recovery = _events(engine, unit_id)[-1]
    assert recovery.kind == "recovered_stale"
    payload = json.loads(recovery.payload_json)
    assert payload["stale_after_minutes"] == _STALE_AFTER.total_seconds() / 60
    assert dt.datetime.fromisoformat(payload["last_started_at"]) == stranded_at


def test_stale_recovery_at_the_cap_is_permanent(engine: Engine, handler: StubStageHandler, repository) -> None:
    """A stranded unit whose claimed attempts reached the cap is failed with no wait."""
    unit_id = handler.enqueue("crashing")
    _set(engine, unit_id, attempts=_CAP - 1)
    _claim(engine, repository)
    _set(engine, unit_id, started_at=_NOW - _STALE_AFTER - dt.timedelta(seconds=1))

    assert _recover(engine, repository) == [unit_id]

    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.FAILED
    assert row.earliest_next_attempt_at is None
    assert row.finished_at == _NOW
    assert row.attempts == _CAP
    assert _events(engine, unit_id)[-1].kind == "recovered_stale"


def test_crash_recover_claim_loop_reaches_the_bound(
    engine: Engine, handler: StubStageHandler, repository, clock: _Clock
) -> None:
    """A unit that crashes its worker every time is recovered until the cap, then left permanent."""
    unit_id = handler.enqueue("crash-loop")

    claims = 0
    while _claim(engine, repository) == unit_id:
        claims += 1
        _set(engine, unit_id, started_at=clock.now - _STALE_AFTER - dt.timedelta(seconds=1))
        _recover(engine, repository)
        clock.advance(seconds=1)

    assert claims == _CAP
    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.FAILED
    assert row.earliest_next_attempt_at is None
    assert row.attempts == _CAP
    assert _recover(engine, repository) == [], "a further sweep finds nothing running"
    assert _row(engine, unit_id).status is WorkUnitStatus.FAILED


def test_stale_threshold_shorter_than_timeout_plus_drain_is_refused() -> None:
    """A stale window a live runner could still be inside fails startup validation."""
    with pytest.raises(StartupValidationError, match="stale threshold"):
        require_stale_threshold(
            stale_after=dt.timedelta(minutes=10),
            timeout=dt.timedelta(seconds=600),
            drain_timeout=30.0,
            cancel_grace=5.0,
        )

    require_stale_threshold(
        stale_after=dt.timedelta(minutes=30),
        timeout=dt.timedelta(seconds=600),
        drain_timeout=30.0,
        cancel_grace=5.0,
    )


# --- operator retry ------------------------------------------------------------------


def _seed_terminal(engine: Engine, handler: StubStageHandler, status: WorkUnitStatus, *, with_wait: bool) -> int:
    """Seed a terminal unit on attempt 2 carrying error fields and a finished time."""
    unit_id = handler.enqueue("terminal", status=status.value)
    _set(
        engine,
        unit_id,
        attempts=2,
        error_code="stub_error",
        error_message="earlier failure",
        last_error_at=_NOW - dt.timedelta(minutes=1),
        finished_at=None if with_wait else _NOW - dt.timedelta(minutes=1),
        earliest_next_attempt_at=_NOW + dt.timedelta(hours=1) if with_wait else None,
    )
    return unit_id


@pytest.mark.parametrize(
    ("status", "with_wait"),
    [
        (WorkUnitStatus.FAILED, True),
        (WorkUnitStatus.FAILED, False),
        (WorkUnitStatus.CANCELLED, False),
        (WorkUnitStatus.TIMED_OUT, False),
    ],
    ids=["failed-retryable", "failed-permanent", "cancelled", "timed-out"],
)
def test_operator_retry_returns_the_unit_to_queued(
    engine: Engine, handler: StubStageHandler, repository, status: WorkUnitStatus, with_wait: bool
) -> None:
    """A retry clears the wait, the error fields, and the finished time, keeps attempts, and records why and who."""
    unit_id = _seed_terminal(engine, handler, status, with_wait=with_wait)

    _transition(engine, unit_id, lambda s, row: repository.requeue(s, row, reason="operator_retry", principal="op"))

    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.QUEUED
    assert row.earliest_next_attempt_at is None
    assert (row.error_code, row.error_message, row.last_error_at, row.finished_at) == (None, None, None, None)
    assert row.attempts == 2
    assert row.queued_at == _NOW
    requeued = [event for event in _events(engine, unit_id) if event.kind == "requeued"]
    assert len(requeued) == 1
    assert json.loads(requeued[0].payload_json) == {
        "kind": "requeued",
        "requeue_reason": "operator_retry",
        "principal": "op",
    }


@pytest.mark.parametrize("status", [WorkUnitStatus.QUEUED, WorkUnitStatus.RUNNING, WorkUnitStatus.SUCCEEDED])
def test_operator_retry_of_a_non_terminal_or_succeeded_unit_is_refused(
    engine: Engine, handler: StubStageHandler, repository, status: WorkUnitStatus
) -> None:
    """A retry outside the terminal-but-not-succeeded set raises ``job_not_retryable`` and changes nothing."""
    unit_id = handler.enqueue("ineligible", status=status.value)
    before = (_snapshot(engine, unit_id), _kinds(engine, unit_id))

    with pytest.raises(
        IneligibleTransitionError, match=f"cannot retry a work-unit in status '{status.value}'"
    ) as info:
        _transition(
            engine, unit_id, lambda s, row: repository.requeue(s, row, reason="operator_retry", principal="op")
        )

    assert info.value.code == "job_not_retryable"
    assert (_snapshot(engine, unit_id), _kinds(engine, unit_id)) == before


def test_attempt_numbering_continues_across_a_retry(engine: Engine, handler: StubStageHandler, repository) -> None:
    """A unit failed on attempt 2 is still on 2 after the retry, on 3 after the claim, and its events say 3."""
    unit_id = _seed_terminal(engine, handler, WorkUnitStatus.FAILED, with_wait=False)

    _transition(engine, unit_id, lambda s, row: repository.requeue(s, row, reason="operator_retry", principal="op"))
    assert _row(engine, unit_id).attempts == 2
    assert _claim(engine, repository) == unit_id
    assert _row(engine, unit_id).attempts == 3
    _finalize(engine, repository, unit_id, _RETRYABLE)

    attempts_after_claim = [event.attempt for event in _events(engine, unit_id)[-2:]]
    assert attempts_after_claim == [3, 3]


def test_a_retried_unit_at_the_cap_runs_exactly_once_more(
    engine: Engine, handler: StubStageHandler, repository
) -> None:
    """A retry of a unit at the cap grants one claim; a retryable failure on it is permanent again."""
    unit_id = handler.enqueue("capped", status=WorkUnitStatus.FAILED.value)
    _set(engine, unit_id, attempts=_CAP, finished_at=_NOW - dt.timedelta(minutes=1))

    _transition(engine, unit_id, lambda s, row: repository.requeue(s, row, reason="operator_retry", principal="op"))
    assert (_row(engine, unit_id).status, _row(engine, unit_id).attempts) == (WorkUnitStatus.QUEUED, _CAP)
    assert _claim(engine, repository) == unit_id
    assert _row(engine, unit_id).attempts == _CAP + 1
    _finalize(engine, repository, unit_id, _RETRYABLE)

    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.FAILED
    assert row.earliest_next_attempt_at is None
    assert row.finished_at == _NOW
    assert json.loads(_events(engine, unit_id)[-1].payload_json)["retryable"] is False
    assert _claim(engine, repository) is None, "it ran exactly once after the retry"


# --- operator cancel -----------------------------------------------------------------


def test_cancel_of_a_failed_unit_awaiting_retry_clears_the_wait_and_keeps_the_error(
    engine: Engine, handler: StubStageHandler, repository, clock: _Clock
) -> None:
    """A cancel of a retry-waiting unit drops the wait, keeps the error fields, and the unit never runs."""
    unit_id = _seed_terminal(engine, handler, WorkUnitStatus.FAILED, with_wait=True)

    _transition(engine, unit_id, lambda s, row: repository.cancel(s, row))

    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.CANCELLED
    assert row.earliest_next_attempt_at is None
    assert (row.error_code, row.error_message) == ("stub_error", "earlier failure")
    assert row.last_error_at == _NOW - dt.timedelta(minutes=1)
    assert row.finished_at == _NOW
    cancelled = _events(engine, unit_id)[-1]
    assert cancelled.kind == "cancelled"
    assert json.loads(cancelled.payload_json)["cancellation_reason"] == "operator_cancel"
    clock.advance(hours=2)
    assert _claim(engine, repository) is None


def test_cancel_of_a_permanently_failed_unit(engine: Engine, handler: StubStageHandler, repository) -> None:
    """A permanent failure is cancellable; its error fields survive and a cancel event is recorded."""
    unit_id = _seed_terminal(engine, handler, WorkUnitStatus.FAILED, with_wait=False)

    _transition(engine, unit_id, lambda s, row: repository.cancel(s, row))

    row = _row(engine, unit_id)
    assert row.status is WorkUnitStatus.CANCELLED
    assert (row.error_code, row.error_message) == ("stub_error", "earlier failure")
    assert _kinds(engine, unit_id)[-1] == "cancelled"


@pytest.mark.parametrize("status", [WorkUnitStatus.SUCCEEDED, WorkUnitStatus.CANCELLED, WorkUnitStatus.TIMED_OUT])
def test_cancel_of_a_terminal_unit_is_refused(
    engine: Engine, handler: StubStageHandler, repository, status: WorkUnitStatus
) -> None:
    """A cancel of a succeeded, cancelled, or timed-out unit raises ``job_not_cancellable`` and changes nothing."""
    unit_id = handler.enqueue("settled", status=status.value)
    before = (_snapshot(engine, unit_id), _kinds(engine, unit_id))

    with pytest.raises(
        IneligibleTransitionError, match=f"cannot cancel a work-unit in status '{status.value}'"
    ) as info:
        _transition(engine, unit_id, lambda s, row: repository.cancel(s, row))

    assert info.value.code == "job_not_cancellable"
    assert (_snapshot(engine, unit_id), _kinds(engine, unit_id)) == before


def test_cancel_in_a_stage_declared_non_cancellable_part_is_refused_and_the_unit_continues(
    engine: Engine, clock: _Clock
) -> None:
    """A running unit inside the stage's declared non-cancellable part keeps running to its own outcome."""
    handler = StubStageHandler(
        engine,
        clock=clock,
        retry_base_delay_seconds=_BASE_DELAY,
        retry_max_attempts=_CAP,
        cancellable_labels={"other"},
    )
    repository = handler.repository
    unit_id = handler.enqueue("uploading")
    _claim(engine, repository)

    with pytest.raises(IneligibleTransitionError) as info:
        _transition(engine, unit_id, lambda s, row: repository.cancel(s, row, cancellable=handler.cancellable))

    assert info.value.code == "job_not_cancellable"
    assert _row(engine, unit_id).status is WorkUnitStatus.RUNNING
    assert "cancelled" not in _kinds(engine, unit_id)
    assert _finalize(engine, repository, unit_id, TerminalOutcome(WorkUnitStatus.SUCCEEDED)) is True
    assert _row(engine, unit_id).status is WorkUnitStatus.SUCCEEDED
