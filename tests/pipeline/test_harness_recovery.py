"""Harness stale-unit recovery test.

Covers the spec requirement that a work-unit left running by an interrupted
runtime is recoverable and that recovery records its cause in the transition
event.
"""

from __future__ import annotations

import datetime as dt
import json

from sqlmodel import Session, select

from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.harness import StageHarness
from aizk.pipeline.lifecycle import WorkUnitStatus

from ._stub_repository import StubStageRepository, create_stub_engine


def test_stale_unit_recovered_with_cause() -> None:
    """A stranded running unit is reclaimed and its recovery cause is recorded.

    A unit is forced into ``running`` with a backdated ``started_at`` to mimic a
    crash. After the harness runs a stale-recovery sweep, the unit has left the
    running state and a transition event records the recovery cause.
    """
    engine = create_stub_engine()
    repo = StubStageRepository(engine, stale_after=dt.timedelta(minutes=5))
    unit_id = repo.enqueue("stranded")
    repo.force_running_stale(unit_id)
    assert repo.get_status(unit_id) == WorkUnitStatus.RUNNING.value, "precondition: stranded running"

    harness = StageHarness(repo, engine, poll_interval=0.01)

    recovered = harness.recover_stale()

    assert recovered == 1, "the stranded unit was recovered"
    assert repo.get_status(unit_id) != WorkUnitStatus.RUNNING.value, "unit left the running state"

    with Session(engine) as session:
        events = list(
            session.exec(
                select(PipelineEvent)
                .where(PipelineEvent.work_unit_ref == str(unit_id))
                .where(PipelineEvent.to_status == WorkUnitStatus.QUEUED.value)
                .order_by(PipelineEvent.event_id)
            )
        )
    assert events, "a transition event recorded the recovery"
    recovery = events[-1]
    assert recovery.from_status == WorkUnitStatus.RUNNING.value, "recovery transitioned out of running"
    payload = json.loads(recovery.payload_json)
    assert payload.get("cause") == "worker_stale_running", "the recovery event records its cause"
