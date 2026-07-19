"""Action-equivalence tests for the console's bulk actions.

A retry or cancel applied through the console must produce the same work-unit state
transition and the same durable lifecycle event as the stage's own action pathway —
the console is thin command dispatch to the same domain helper, not a second
implementation. This pins that equivalence for both graph stages (contextualization
and extraction), retry and cancel, against the lifted
:mod:`aizk.graph.job_actions` helpers the JSON API and worker also call.
"""

from __future__ import annotations

from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.job_actions import (
    apply_contextualization_cancel,
    apply_contextualization_retry,
    apply_extraction_cancel,
    apply_extraction_retry,
)
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus

_EVENT_FIELDS = ("stage", "kind", "from_status", "to_status", "attempt", "payload_json")


def _terminal_event(session: Session, stage: str, job_id: int, to_status: WorkUnitStatus) -> PipelineEvent:
    """Return the single lifecycle event transitioning a stage's work-unit to ``to_status``."""
    events = session.exec(
        select(PipelineEvent)
        .where(PipelineEvent.stage == stage)
        .where(PipelineEvent.work_unit_ref == str(job_id))
        .where(PipelineEvent.to_status == to_status.value)
    ).all()
    assert len(events) == 1, f"expected exactly one {to_status.value} event, got {len(events)}"
    return events[0]


def _assert_equivalent(
    session: Session, stage: str, console_id: int, helper_id: int, to_status: WorkUnitStatus
) -> None:
    """Assert the console-path and helper-path units reached the same status and event."""
    session.expire_all()
    model = ContextualizationJob if stage == CONTEXTUALIZATION_STAGE else ExtractionJob
    assert session.get(model, console_id).status is to_status
    assert session.get(model, helper_id).status is to_status
    console_event = _terminal_event(session, stage, console_id, to_status)
    helper_event = _terminal_event(session, stage, helper_id, to_status)
    assert {f: getattr(console_event, f) for f in _EVENT_FIELDS} == {
        f: getattr(helper_event, f) for f in _EVENT_FIELDS
    }


# --- contextualization --------------------------------------------------------


def test_console_cancel_equals_the_contextualization_cancel_pathway(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """A console cancel and a direct ``apply_contextualization_cancel`` match."""
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
    apply_contextualization_cancel(db_session, via_helper)
    db_session.commit()

    _assert_equivalent(db_session, CONTEXTUALIZATION_STAGE, via_console.id, via_helper.id, WorkUnitStatus.CANCELLED)


def test_console_retry_equals_the_contextualization_retry_pathway(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """A console retry and a direct ``apply_contextualization_retry`` match."""
    source = seed_source(db_session, karakeep_id="bm_equiv_retry", title="Equivalence Retry Doc")
    via_console = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=93, status=WorkUnitStatus.FAILED
    )
    via_helper = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=94, status=WorkUnitStatus.FAILED
    )

    response = client.post(
        "/ui/tasks/contextualization/actions", data={"action": "retry", "job_ids": [via_console.id]}
    )
    assert response.status_code == 200

    apply_contextualization_retry(db_session, via_helper)
    db_session.commit()

    _assert_equivalent(db_session, CONTEXTUALIZATION_STAGE, via_console.id, via_helper.id, WorkUnitStatus.QUEUED)


# --- extraction ---------------------------------------------------------------


def test_console_cancel_equals_the_extraction_cancel_pathway(
    client: TestClient, db_session: Session, seed_source, seed_extraction_job
) -> None:
    """A console cancel and a direct ``apply_extraction_cancel`` match."""
    source = seed_source(db_session, karakeep_id="bm_equiv_ext", title="Extraction Equivalence Doc")
    via_console = seed_extraction_job(
        db_session, source_id=source.source_id, status=WorkUnitStatus.QUEUED, idempotency_key="equiv:console"
    )
    via_helper = seed_extraction_job(
        db_session, source_id=source.source_id, status=WorkUnitStatus.QUEUED, idempotency_key="equiv:helper"
    )

    response = client.post("/ui/tasks/extraction/actions", data={"action": "cancel", "job_ids": [via_console.id]})
    assert response.status_code == 200

    apply_extraction_cancel(db_session, via_helper)
    db_session.commit()

    _assert_equivalent(db_session, EXTRACTION_STAGE, via_console.id, via_helper.id, WorkUnitStatus.CANCELLED)


def test_console_retry_equals_the_extraction_retry_pathway(
    client: TestClient, db_session: Session, seed_source, seed_extraction_job
) -> None:
    """A console retry and a direct ``apply_extraction_retry`` match."""
    source = seed_source(db_session, karakeep_id="bm_equiv_ext_retry", title="Extraction Retry Doc")
    via_console = seed_extraction_job(
        db_session, source_id=source.source_id, status=WorkUnitStatus.FAILED, idempotency_key="equiv:console"
    )
    via_helper = seed_extraction_job(
        db_session, source_id=source.source_id, status=WorkUnitStatus.FAILED, idempotency_key="equiv:helper"
    )

    response = client.post("/ui/tasks/extraction/actions", data={"action": "retry", "job_ids": [via_console.id]})
    assert response.status_code == 200

    apply_extraction_retry(db_session, via_helper)
    db_session.commit()

    _assert_equivalent(db_session, EXTRACTION_STAGE, via_console.id, via_helper.id, WorkUnitStatus.QUEUED)
