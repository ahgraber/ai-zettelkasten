"""Unit tests for the graph stages' capacity primitives (``aizk.graph.capacity``).

Covers what counts as a stage's actionable backlog and how a declared limit
turns that count into a refusal or a batch headroom. The per-write-site evidence
that each enqueue primitive honors the limit lives with those primitives'
tests (`test_enqueue.py`, `test_extraction_workunit.py`).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine

from aizk.graph.capacity import (
    StageAtCapacityError,
    actionable_backlog,
    check_capacity,
    headroom,
    within_headroom,
)
from aizk.graph.datamodel import ExtractionJob
from aizk.pipeline.lifecycle import WorkUnitStatus

_RETRY_AT = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def _make_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'capacity.db'}")
    SQLModel.metadata.create_all(engine, tables=[ExtractionJob.__table__])
    return engine


def _add_unit(
    session: Session,
    status: WorkUnitStatus,
    *,
    earliest_next_attempt_at: dt.datetime | None = None,
) -> UUID:
    source_id = uuid4()
    session.add(
        ExtractionJob(
            idempotency_key=f"source:{source_id}",
            source_id=source_id,
            status=status,
            earliest_next_attempt_at=earliest_next_attempt_at,
        )
    )
    return source_id


@pytest.mark.parametrize(
    ("status", "earliest_next_attempt_at", "counts"),
    [
        (WorkUnitStatus.QUEUED, None, True),
        (WorkUnitStatus.FAILED, _RETRY_AT, True),
        (WorkUnitStatus.FAILED, None, False),
        (WorkUnitStatus.RUNNING, None, False),
        (WorkUnitStatus.SUCCEEDED, None, False),
        (WorkUnitStatus.CANCELLED, None, False),
        (WorkUnitStatus.TIMED_OUT, None, False),
    ],
    ids=["queued", "failed-awaiting-retry", "failed-permanent", "running", "succeeded", "cancelled", "timed-out"],
)
def test_actionable_backlog_counts_only_work_the_stage_still_owes(
    tmp_path: Path,
    status: WorkUnitStatus,
    earliest_next_attempt_at: dt.datetime | None,
    counts: bool,
) -> None:
    """Queued work and failures awaiting retry are backlog; finished and exhausted work is not."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_unit(session, status, earliest_next_attempt_at=earliest_next_attempt_at)
        session.commit()

        assert actionable_backlog(session, ExtractionJob) == (1 if counts else 0)


def test_check_capacity_refuses_once_the_backlog_reaches_the_limit(tmp_path: Path) -> None:
    """At the limit the next new unit is refused, and the refusal names the stage and reading."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_unit(session, WorkUnitStatus.QUEUED)
        _add_unit(session, WorkUnitStatus.QUEUED)
        session.commit()

        check_capacity(session, ExtractionJob, stage="extraction", limit=3)

        with pytest.raises(StageAtCapacityError) as excinfo:
            check_capacity(session, ExtractionJob, stage="extraction", limit=2)
        assert excinfo.value.stage == "extraction"
        assert excinfo.value.depth == 2
        assert excinfo.value.limit == 2


@pytest.mark.parametrize("limit", [0, -1])
def test_an_undeclared_limit_never_refuses(tmp_path: Path, limit: int) -> None:
    """A stage that declares no limit accepts work whatever its backlog."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        for _ in range(5):
            _add_unit(session, WorkUnitStatus.QUEUED)
        session.commit()

        check_capacity(session, ExtractionJob, stage="extraction", limit=limit)
        assert headroom(session, ExtractionJob, limit=limit) is None


def test_headroom_is_the_room_left_under_the_limit(tmp_path: Path) -> None:
    """Headroom is the limit less the backlog, and never goes negative on an overshoot."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        for _ in range(3):
            _add_unit(session, WorkUnitStatus.QUEUED)
        session.commit()

        assert headroom(session, ExtractionJob, limit=5) == 2
        assert headroom(session, ExtractionJob, limit=2) == 0


def test_within_headroom_truncates_the_batch_and_reports_what_is_left(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A batch larger than the headroom is cut to it, and the dropped remainder is logged."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_unit(session, WorkUnitStatus.QUEUED)
        session.commit()

        with caplog.at_level("INFO", logger="aizk.graph.capacity"):
            admitted = within_headroom(session, ExtractionJob, "abcde", stage="extraction", limit=3)

        assert admitted == ["a", "b"]
        assert "2 of 5" in caplog.text
        assert "3 left pending" in caplog.text


def test_within_headroom_passes_a_batch_that_fits(tmp_path: Path) -> None:
    """A batch inside the headroom is admitted whole, with no truncation."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        assert within_headroom(session, ExtractionJob, [1, 2], stage="extraction", limit=5) == [1, 2]
        assert within_headroom(session, ExtractionJob, [1, 2], stage="extraction", limit=0) == [1, 2]
