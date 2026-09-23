"""Cross-stage lifecycle tests: three stages, one retry policy, one clock, one result.

Covers the runtime spec's requirement that lifecycle behavior is identical
across stages. The contextualization and extraction handlers and the stub stage
are driven through the same operations under the same policy and clock, and
each is asserted against the same expected ``(status, wait, attempts, kind)``.
Failures are driven through ``finalize`` with a terminal outcome, not through
``execute``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import datetime as dt
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.graph.extraction_handler import ExtractionStageHandler
from aizk.graph.extraction_workunit import enqueue_extraction
from aizk.graph.handler import ContextualizationStageHandler
from aizk.graph.llm import STUB_MODEL_CONFIG, StubLLMClient
from aizk.graph.workunit import enqueue_document
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.pipeline.stage import HandlerBase
from tests.fakes.markdown import StubMarkdownSource

from ._stub_handler import StubStageHandler, StubWorkUnit, create_stub_file_engine

_NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.timezone.utc)
_CAP = 3
_BASE_DELAY = 10.0
_STALE_AFTER = dt.timedelta(minutes=5)
_POLICY = {
    "retry_max_attempts": _CAP,
    "retry_base_delay_seconds": _BASE_DELAY,
    "stale_after_minutes": _STALE_AFTER.total_seconds() / 60,
}
_RETRYABLE = TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE)
_PERMANENT = TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.PERMANENT)


def _clock() -> dt.datetime:
    return _NOW


def _naive(moment: dt.datetime) -> dt.datetime:
    return moment.replace(tzinfo=None)


class _AlwaysCurrent:
    """A freshness stub treating every output as current; never called here."""

    def is_current(self, session: Session, source_id: UUID, conversion_output_id: int) -> bool:
        return True


class _SilentExtractor:
    """An extractor detecting nothing; never called here."""

    extractor_version = "stub/v1"

    def extract(self, text: str) -> Sequence[object]:
        return []


@dataclass
class _Stage:
    """One stage under test: its handler, engine, table, and a seeder of queued units."""

    handler: HandlerBase
    engine: Engine
    model: type
    seed: Callable[[int], list[int]]


def _graph_engine(tmp_path: Path, name: str, tables: list) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False, "timeout": 30})
    SQLModel.metadata.create_all(engine, tables=tables)
    return engine


def _stamp_submission_order(engine: Engine, model: type, ids: list[int]) -> None:
    """Give the seeded units strictly increasing ``queued_at`` values under the fixed clock."""
    with Session(engine) as session:
        for offset, unit_id in enumerate(ids):
            row = session.get(model, unit_id)
            row.queued_at = _NOW - dt.timedelta(seconds=len(ids) - offset)
            session.add(row)
        session.commit()


def _contextualization(tmp_path: Path) -> _Stage:
    engine = _graph_engine(tmp_path, "contextualization.db", [ContextualizationJob.__table__, PipelineEvent.__table__])
    handler = ContextualizationStageHandler(
        engine,
        StubLLMClient(),
        StubMarkdownSource(),
        _AlwaysCurrent(),
        model_config=STUB_MODEL_CONFIG,
        clock=_clock,
        **_POLICY,
    )

    def _seed(count: int) -> list[int]:
        ids: list[int] = []
        with Session(engine) as session:
            for output_id in range(1, count + 1):
                job, _ = enqueue_document(
                    session,
                    conversion_output_id=output_id,
                    source_id=uuid4(),
                    queue_max_depth=0,
                    origin="intake",
                    principal="test",
                )
                ids.append(job.id)
            session.commit()
        _stamp_submission_order(engine, ContextualizationJob, ids)
        return ids

    return _Stage(handler, engine, ContextualizationJob, _seed)


def _extraction(tmp_path: Path) -> _Stage:
    engine = _graph_engine(tmp_path, "extraction.db", [ExtractionJob.__table__, PipelineEvent.__table__])
    handler = ExtractionStageHandler(engine, _SilentExtractor(), input_policy="raw", clock=_clock, **_POLICY)

    def _seed(count: int) -> list[int]:
        ids: list[int] = []
        with Session(engine) as session:
            for _ in range(count):
                job, _ = enqueue_extraction(
                    session, source_id=uuid4(), queue_max_depth=0, origin="intake", principal="test"
                )
                ids.append(job.id)
            session.commit()
        _stamp_submission_order(engine, ExtractionJob, ids)
        return ids

    return _Stage(handler, engine, ExtractionJob, _seed)


def _stub(tmp_path: Path) -> _Stage:
    engine = create_stub_file_engine(str(tmp_path / "stub.db"), create_schema=True)
    handler = StubStageHandler(
        engine,
        clock=_clock,
        stale_after=_STALE_AFTER,
        retry_base_delay_seconds=_BASE_DELAY,
        retry_max_attempts=_CAP,
    )

    def _seed(count: int) -> list[int]:
        ids = [handler.enqueue(f"u{index}") for index in range(count)]
        _stamp_submission_order(engine, StubWorkUnit, ids)
        return ids

    return _Stage(handler, engine, StubWorkUnit, _seed)


@pytest.fixture(params=[_contextualization, _extraction, _stub], ids=["contextualization", "extraction", "stub"])
def stage(request: pytest.FixtureRequest, tmp_path: Path) -> _Stage:
    built = request.param(tmp_path)
    yield built
    built.engine.dispose()


def _observe(stage: _Stage, unit_id: int) -> tuple[Any, Any, int, str]:
    """Return ``(status, wait, attempts, last event kind)`` for ``unit_id``."""
    with Session(stage.engine) as session:
        row = session.get(stage.model, unit_id)
        assert row is not None
        last = session.exec(
            select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(unit_id)).order_by(PipelineEvent.event_id)
        ).all()[-1]
        return WorkUnitStatus(row.status), row.earliest_next_attempt_at, row.attempts, last.kind


def _claim(stage: _Stage) -> int | None:
    with Session(stage.engine) as session:
        handle = stage.handler.claim_next(session)
        session.commit()
    return handle


def _finalize(stage: _Stage, unit_id: int, outcome: TerminalOutcome) -> None:
    with Session(stage.engine) as session:
        stage.handler.finalize(session, unit_id, outcome)
        session.commit()


def _set(stage: _Stage, unit_id: int, **fields: Any) -> None:
    with Session(stage.engine) as session:
        row = session.get(stage.model, unit_id)
        for name, value in fields.items():
            setattr(row, name, value)
        session.add(row)
        session.commit()


def test_claim_order_is_the_same(stage: _Stage) -> None:
    """Three queued units are claimed earliest first, each landing on attempt 1."""
    ids = stage.seed(3)

    claimed = [_claim(stage) for _ in range(3)]

    assert claimed == ids
    assert [_observe(stage, unit_id) for unit_id in ids] == [(WorkUnitStatus.RUNNING, None, 1, "claimed")] * 3


def test_retryable_failure_below_the_cap_waits_the_same_way(stage: _Stage) -> None:
    """A retryable failure on attempt 1 waits ``base * 2**1`` under every stage."""
    (unit_id,) = stage.seed(1)
    _claim(stage)

    _finalize(stage, unit_id, _RETRYABLE)

    expected_wait = _naive(_NOW + dt.timedelta(seconds=_BASE_DELAY * 2))
    assert _observe(stage, unit_id) == (WorkUnitStatus.FAILED, expected_wait, 1, "failed")


def test_retryable_failure_at_the_cap_becomes_permanent_the_same_way(stage: _Stage) -> None:
    """A retryable failure on the last allowed attempt has no wait under every stage."""
    (unit_id,) = stage.seed(1)
    _set(stage, unit_id, attempts=_CAP - 1)
    _claim(stage)

    _finalize(stage, unit_id, _RETRYABLE)

    assert _observe(stage, unit_id) == (WorkUnitStatus.FAILED, None, _CAP, "failed")


def test_permanent_failure_is_recorded_the_same_way(stage: _Stage) -> None:
    """A permanent failure has no wait and keeps its single attempt under every stage."""
    (unit_id,) = stage.seed(1)
    _claim(stage)

    _finalize(stage, unit_id, _PERMANENT)

    assert _observe(stage, unit_id) == (WorkUnitStatus.FAILED, None, 1, "failed")


def test_interrupted_unit_is_recovered_the_same_way(stage: _Stage) -> None:
    """A unit stranded past the stale window is failed with an immediate wait and a recovery event."""
    (unit_id,) = stage.seed(1)
    _claim(stage)
    _set(stage, unit_id, started_at=_NOW - _STALE_AFTER - dt.timedelta(seconds=1))
    # The runtime that held the unit stopped abruptly: its claim token is gone,
    # so the sweep sees a unit nobody holds.
    stage.handler.cleanup(unit_id)

    with Session(stage.engine) as session:
        recovered = stage.handler.recover_stale(session)
        session.commit()

    assert recovered == [unit_id]
    assert _observe(stage, unit_id) == (WorkUnitStatus.FAILED, _naive(_NOW), 1, "recovered_stale")


def test_operator_retry_has_the_same_effect(stage: _Stage) -> None:
    """A retry of a failed unit returns it to queued with no wait, the same attempt count, and a requeued event."""
    (unit_id,) = stage.seed(1)
    _claim(stage)
    _finalize(stage, unit_id, _RETRYABLE)

    with Session(stage.engine) as session:
        row = session.get(stage.model, unit_id)
        stage.handler.repository.requeue(session, row, reason="operator_retry", principal="op")
        session.commit()

    assert _observe(stage, unit_id) == (WorkUnitStatus.QUEUED, None, 1, "requeued")
