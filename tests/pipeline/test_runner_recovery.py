"""Runner stale-unit recovery test.

Covers the spec requirement that a work-unit left running by an interrupted
runtime is recoverable and that recovery records its cause in the transition
event.
"""

from __future__ import annotations

import datetime as dt
import json

from sqlmodel import Session, select

from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.runner import StageRunner

from ._stub_handler import StubStageHandler, StubWorkUnit, create_stub_engine


def test_stale_unit_recovered_with_cause() -> None:
    """A stranded running unit is reclaimed as a retryable failure and its cause is recorded.

    A unit is forced into ``running`` with a backdated ``started_at`` to mimic a
    crash. After the runner runs a stale-recovery sweep, the unit is ``failed``
    with an immediate retry wait and the stale error code, and a
    ``recovered_stale`` event records the window and the interrupted start.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, stale_after=dt.timedelta(minutes=5))
    unit_id = handler.enqueue("stranded")
    handler.force_running_stale(unit_id)
    assert handler.get_status(unit_id) == WorkUnitStatus.RUNNING.value, "precondition: stranded running"

    runner = StageRunner(handler, engine, poll_interval=0.01)

    recovered = runner.recover_stale()

    assert recovered == 1, "the stranded unit was recovered"
    with Session(engine) as session:
        unit = session.get(StubWorkUnit, unit_id)
        assert unit is not None
        assert unit.status is WorkUnitStatus.FAILED, "recovery leaves the unit failed"
        assert unit.earliest_next_attempt_at is not None, "recovery sets an immediate retry wait"
        assert unit.error_code == "worker_stale_running", "recovery names its cause on the row"
        events = list(
            session.exec(
                select(PipelineEvent)
                .where(PipelineEvent.work_unit_ref == str(unit_id))
                .where(PipelineEvent.kind == "recovered_stale")
                .order_by(PipelineEvent.event_id)
            )
        )
    assert len(events) == 1, "one transition event recorded the recovery"
    recovery = events[0]
    assert recovery.from_status == WorkUnitStatus.RUNNING.value, "recovery transitioned out of running"
    assert recovery.to_status == WorkUnitStatus.FAILED.value
    payload = json.loads(recovery.payload_json)
    assert payload["stale_after_minutes"] == 5.0, "the recovery event records the stale window"
    assert payload["last_started_at"] is not None, "the recovery event records the interrupted start"
