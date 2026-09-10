"""Tests for the static stage definition and the shared enqueue over it.

Covers the runtime spec's requirement that a stage publishes a definition
resolvable without a runtime, and that a unit created through the definition
equals one created through the stage's own enqueue.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, select

from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.stage import enqueue

from ._stub_handler import (
    STUB_DEFINITION,
    StubEventKind,
    StubParams,
    StubStageHandler,
    StubWorkUnit,
    create_stub_file_engine,
)

_SOURCE_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_definition_resolves_without_a_runtime() -> None:
    """The definition's name, parameter shape, key derivation, record type, and kinds need no engine."""
    params = STUB_DEFINITION.params(source_id=_SOURCE_ID, label="doc")

    assert STUB_DEFINITION.name == "stub"
    assert isinstance(params, StubParams)
    assert STUB_DEFINITION.unit_key(params) == f"stub:{_SOURCE_ID}"
    assert STUB_DEFINITION.work_unit_model is StubWorkUnit
    assert STUB_DEFINITION.event_kinds is StubEventKind
    assert STUB_DEFINITION.pending is None


def _observe(engine, unit_id: int) -> tuple:
    """Return the persisted lifecycle fields and creation event of ``unit_id``."""
    with Session(engine) as session:
        row = session.get(StubWorkUnit, unit_id)
        assert row is not None
        (event,) = session.exec(select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(unit_id))).all()
        payload = json.loads(event.payload_json)
        return (
            row.idempotency_key,
            row.status,
            row.attempts,
            row.earliest_next_attempt_at,
            event.kind,
            payload["origin"],
        )


def test_enqueue_through_the_definition_equals_the_stage_enqueue(tmp_path: Path) -> None:
    """The same parameters produce the same unit whether enqueued through the definition or the stage."""
    engine_a = create_stub_file_engine(str(tmp_path / "definition.db"), create_schema=True)
    engine_b = create_stub_file_engine(str(tmp_path / "stage.db"), create_schema=True)
    params = StubParams(source_id=_SOURCE_ID, label="doc")

    with Session(engine_a) as session:
        unit, created = enqueue(session, STUB_DEFINITION, params, origin="intake", principal="stub", queue_max_depth=0)
        session.commit()
        via_definition = (unit.id, created)
        again, created_again = enqueue(
            session, STUB_DEFINITION, params, origin="intake", principal="stub", queue_max_depth=0
        )
        assert (again.id, created_again) == (unit.id, False), "a second enqueue reuses the unit"
    handler = StubStageHandler(engine_b)
    via_stage = handler.enqueue("doc", source_id=_SOURCE_ID)
    assert handler.enqueue("doc", source_id=_SOURCE_ID) == via_stage, "a second stage enqueue reuses the unit"

    assert via_definition == (via_stage, True), "both paths return the same identity"
    observed_a = _observe(engine_a, via_definition[0])
    observed_b = _observe(engine_b, via_stage)
    assert observed_a == observed_b
    assert observed_a == (f"stub:{_SOURCE_ID}", WorkUnitStatus.QUEUED, 0, None, "queued", "intake")
    engine_a.dispose()
    engine_b.dispose()
