"""Action-equivalence tests for the console's bulk actions.

A retry or cancel applied through the console must produce the same work-unit
state transition and the same durable lifecycle event as the stage's own action
pathway — the console is thin command dispatch to the same domain helper, not a
second implementation. This pins that equivalence for the contextualization
stage's cancel.
"""

from __future__ import annotations

from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.graph.api.routes import _apply_cancel
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus


def _cancel_event(session: Session, job_id: int) -> PipelineEvent:
    """Return the single cancelled lifecycle event recorded for a work-unit."""
    events = session.exec(
        select(PipelineEvent)
        .where(PipelineEvent.stage == CONTEXTUALIZATION_STAGE)
        .where(PipelineEvent.work_unit_ref == str(job_id))
        .where(PipelineEvent.to_status == WorkUnitStatus.CANCELLED.value)
    ).all()
    assert len(events) == 1, f"expected exactly one cancel event, got {len(events)}"
    return events[0]


def test_console_cancel_equals_the_stage_cancel_pathway(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """A console cancel and a direct ``_apply_cancel`` reach the same status and event."""
    source = seed_source(db_session, karakeep_id="bm_equiv", title="Equivalence Doc")
    via_console = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=91, status=WorkUnitStatus.QUEUED
    )
    via_helper = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=92, status=WorkUnitStatus.QUEUED
    )

    response = client.post(
        "/ui/tasks/contextualization/actions", data={"action": "cancel", "job_ids": [via_console.id]}
    )
    assert response.status_code == 200

    # The stage's own pathway: the same domain helper the JSON API and worker use.
    _apply_cancel(db_session, via_helper)
    db_session.commit()

    db_session.expire_all()
    assert db_session.get(ContextualizationJob, via_console.id).status is WorkUnitStatus.CANCELLED
    assert db_session.get(ContextualizationJob, via_helper.id).status is WorkUnitStatus.CANCELLED

    console_event = _cancel_event(db_session, via_console.id)
    helper_event = _cancel_event(db_session, via_helper.id)
    fields = ("stage", "kind", "from_status", "to_status", "attempt", "payload_json")
    assert {f: getattr(console_event, f) for f in fields} == {f: getattr(helper_event, f) for f in fields}
