"""Tests for the handler base's attempt token over the stub stage.

Covers the rule that a process never reclaims or re-claims a unit it still
holds, and that a unit another process reclaimed cannot be finalized by the
execution that lost it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session

from aizk.pipeline.lifecycle import TerminalOutcome, WorkUnitStatus

from ._stub_handler import StubStageHandler, StubWorkUnit, create_stub_file_engine

_STALE_AFTER = dt.timedelta(minutes=5)


def _strand(engine: Engine, unit_id: int) -> None:
    """Backdate a running unit's ``started_at`` past the stale window."""
    with Session(engine) as session:
        row = session.get(StubWorkUnit, unit_id)
        assert row is not None
        row.started_at = dt.datetime.now(dt.timezone.utc) - _STALE_AFTER - dt.timedelta(seconds=1)
        session.add(row)
        session.commit()


def _claim(handler: StubStageHandler, engine: Engine) -> int | None:
    with Session(engine) as session:
        handle = handler.claim_next(session)
        session.commit()
    return handle


def _recover(handler: StubStageHandler, engine: Engine) -> list[int]:
    with Session(engine) as session:
        recovered = handler.recover_stale(session)
        session.commit()
    return recovered


def _row(engine: Engine, unit_id: int) -> StubWorkUnit:
    with Session(engine) as session:
        row = session.get(StubWorkUnit, unit_id)
        assert row is not None
        session.expunge(row)
        return row


def test_a_held_unit_is_neither_recovered_nor_reclaimed_by_its_own_process(tmp_path: Path) -> None:
    """A stale unit this handler still holds is skipped by its sweep and claim until it releases it."""
    engine = create_stub_file_engine(str(tmp_path / "held.db"), create_schema=True)
    handler = StubStageHandler(engine, concurrency_limit=2, stale_after=_STALE_AFTER)
    unit_id = handler.enqueue("held")
    assert _claim(handler, engine) == unit_id
    _strand(engine, unit_id)

    assert _recover(handler, engine) == [], "the sweep leaves a unit this process holds"
    assert _claim(handler, engine) is None, "the claim never takes a unit this process holds"
    assert (_row(engine, unit_id).status, _row(engine, unit_id).attempts) == (WorkUnitStatus.RUNNING, 1)

    handler.cleanup(unit_id)

    assert _recover(handler, engine) == [unit_id], "once released, the same sweep recovers it"
    assert _row(engine, unit_id).status is WorkUnitStatus.FAILED
    engine.dispose()


def test_a_unit_reclaimed_by_another_process_is_not_finalized_by_the_execution_that_lost_it(
    tmp_path: Path,
) -> None:
    """Another process's reclaim moves the row to attempt 2; the first process's finalize writes nothing."""
    db_path = str(tmp_path / "shared.db")
    engine_a = create_stub_file_engine(db_path, create_schema=True)
    engine_b = create_stub_file_engine(db_path, create_schema=False)
    handler_a = StubStageHandler(engine_a, stale_after=_STALE_AFTER)
    handler_b = StubStageHandler(engine_b, stale_after=_STALE_AFTER)
    unit_id = handler_a.enqueue("contended")
    assert _claim(handler_a, engine_a) == unit_id
    _strand(engine_a, unit_id)

    assert _recover(handler_b, engine_b) == [unit_id]
    handler_b.register_behavior(unit_id)
    assert _claim(handler_b, engine_b) == unit_id
    assert _row(engine_b, unit_id).attempts == 2

    with Session(engine_a) as session:
        handler_a.finalize(session, unit_id, TerminalOutcome(WorkUnitStatus.SUCCEEDED))
        session.commit()

    row = _row(engine_b, unit_id)
    assert (row.status, row.attempts, row.finished_at) == (WorkUnitStatus.RUNNING, 2, None)
    engine_a.dispose()
    engine_b.dispose()
