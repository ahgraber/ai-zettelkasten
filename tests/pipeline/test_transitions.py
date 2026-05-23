"""Tests for the generic ``record_transition`` helper and shared event log.

Covers the spec requirements that every work-unit status change is co-committed
with one matching durable event (and neither survives a failed transaction), and
that work-units and events carry a cross-stage source identity resolvable across
stages.

The work-unit stub lives on its own SQLAlchemy ``MetaData`` (not
``SQLModel.metadata``) so it never pollutes the shared metadata that the
migration parity tests compare against ``create_all``.
"""

from __future__ import annotations

import json
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlmodel import Session, select

from aizk.pipeline.events import PipelineEvent, record_transition


class _StubBase(DeclarativeBase):
    """Private declarative base so the stub table stays off ``SQLModel.metadata``."""


class StubWorkUnit(_StubBase):
    """Minimal stage work-unit with a mutable text status, for helper tests."""

    __tablename__ = "_stub_work_units"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column()


class StubTransitionPayload(BaseModel):
    """A stage-defined typed payload model standing in for a real per-kind contract."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["test_transition"] = "test_transition"
    note: str


@pytest.fixture
def wu_engine(engine: Engine) -> Engine:
    """The pipeline engine with the stub work-unit table also created."""
    _StubBase.metadata.create_all(engine)
    return engine


def _make_unit(engine: Engine, status: str = "queued") -> int:
    """Persist a stub work-unit and return its id."""
    with Session(engine) as session:
        unit = StubWorkUnit(status=status)
        session.add(unit)
        session.commit()
        return unit.id


def test_status_and_event_co_committed(wu_engine: Engine) -> None:
    """A status transition writes exactly one matching event, committed together."""
    source = uuid4()
    unit_id = _make_unit(wu_engine, status="queued")

    with Session(wu_engine) as session:
        unit = session.get(StubWorkUnit, unit_id)
        record_transition(
            session,
            unit,
            stage="teststage",
            work_unit_ref=str(unit_id),
            aizk_uuid=source,
            to_status="running",
            kind="test_transition",
            payload=StubTransitionPayload(note="claimed"),
        )
        session.commit()

    with Session(wu_engine) as session:
        unit = session.get(StubWorkUnit, unit_id)
        assert unit.status == "running", "status change is durable"

        events = list(session.exec(select(PipelineEvent).where(PipelineEvent.aizk_uuid == source)))
        assert len(events) == 1, "exactly one event recorded"
        event = events[0]
        assert event.from_status == "queued"
        assert event.to_status == "running"
        assert event.stage == "teststage"
        assert event.work_unit_ref == str(unit_id)
        assert event.kind == "test_transition"
        assert json.loads(event.payload_json) == {"kind": "test_transition", "note": "claimed"}


def test_failed_transaction_leaves_neither(wu_engine: Engine) -> None:
    """A transition whose transaction fails before commit leaves status and event absent."""
    source = uuid4()
    unit_id = _make_unit(wu_engine, status="queued")

    with Session(wu_engine) as session:
        unit = session.get(StubWorkUnit, unit_id)
        record_transition(
            session,
            unit,
            stage="teststage",
            work_unit_ref=str(unit_id),
            aizk_uuid=source,
            to_status="running",
            kind="test_transition",
            payload=StubTransitionPayload(note="claimed"),
        )
        session.rollback()  # transaction fails before commit

    with Session(wu_engine) as session:
        unit = session.get(StubWorkUnit, unit_id)
        assert unit.status == "queued", "status change was rolled back"
        events = list(session.exec(select(PipelineEvent)))
        assert events == [], "no event persisted"


def test_events_resolvable_by_source_across_stages(wu_engine: Engine) -> None:
    """Events for one source across multiple stages are returned together by identity."""
    source = uuid4()
    other_source = uuid4()
    conv_id = _make_unit(wu_engine)
    chunk_id = _make_unit(wu_engine)
    other_id = _make_unit(wu_engine)

    with Session(wu_engine) as session:
        record_transition(
            session,
            session.get(StubWorkUnit, conv_id),
            stage="conversion",
            work_unit_ref=f"job:{conv_id}",
            aizk_uuid=source,
            to_status="running",
            kind="test_transition",
            payload=StubTransitionPayload(note="conv"),
        )
        record_transition(
            session,
            session.get(StubWorkUnit, chunk_id),
            stage="chunking",
            work_unit_ref=f"chunk:{chunk_id}",
            aizk_uuid=source,
            to_status="running",
            kind="test_transition",
            payload=StubTransitionPayload(note="chunk"),
        )
        record_transition(
            session,
            session.get(StubWorkUnit, other_id),
            stage="conversion",
            work_unit_ref=f"job:{other_id}",
            aizk_uuid=other_source,
            to_status="running",
            kind="test_transition",
            payload=StubTransitionPayload(note="other"),
        )
        session.commit()

    with Session(wu_engine) as session:
        events = list(
            session.exec(
                select(PipelineEvent).where(PipelineEvent.aizk_uuid == source).order_by(PipelineEvent.event_id)
            )
        )
        assert len(events) == 2, "only this source's events"
        assert {event.stage for event in events} == {"conversion", "chunking"}, "both stages returned together"
        assert all(event.aizk_uuid == source for event in events)


def test_payload_kind_must_match_transition_kind(wu_engine: Engine) -> None:
    """The helper enforces that the typed payload's kind matches the transition kind."""
    unit_id = _make_unit(wu_engine)
    with Session(wu_engine) as session:
        unit = session.get(StubWorkUnit, unit_id)
        with pytest.raises(ValueError, match="does not match transition kind"):
            record_transition(
                session,
                unit,
                stage="teststage",
                work_unit_ref=str(unit_id),
                aizk_uuid=uuid4(),
                to_status="running",
                kind="some_other_kind",
                payload=StubTransitionPayload(note="mismatch"),
            )
