"""Contract tests for the ConversionJobEvent log produced by API write sites.

These tests pin the spec contract that every API status-mutating endpoint
appends exactly one matching event row in the same transaction as the job
mutation. They exercise the HTTP layer end-to-end (TestClient + real
SQLite via the ``db_session`` fixture) and read the persisted event rows
to verify shape and payload contents.
"""

from __future__ import annotations

import json

from sqlmodel import select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.events import ConversionEventKind, ConversionJobEvent
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus


def _events_for_job(db_session, job_id: int) -> list[ConversionJobEvent]:
    return db_session.exec(
        select(ConversionJobEvent).where(ConversionJobEvent.job_id == job_id).order_by(ConversionJobEvent.id)
    ).all()


def _submit_job(client: TestClient, bookmark_id: str) -> dict:
    resp = client.post(
        "/v1/jobs",
        json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}},
    )
    assert resp.status_code == 201, resp.json()
    return resp.json()


def _force_failed_retryable(db_session, job_id: int) -> None:
    """Backdoor: move a freshly-submitted job into FAILED_RETRYABLE so it is retryable.

    The retry endpoint requires the job to be in a terminal-failed/cancelled state.
    These contract tests don't run a worker, so the test sets the state directly.
    The event-log table is left untouched — only the projection (`status`) is
    mutated — which the tests below leverage to count *only* API-authored events.
    """
    job = db_session.get(ConversionJob, job_id)
    assert job is not None
    job.status = ConversionJobStatus.FAILED_RETRYABLE
    db_session.add(job)
    db_session.commit()


def test_job_submission_emits_queued_event(db_session) -> None:
    """POST /v1/jobs commits the job row and exactly one origin `queued` event.

    The origin event MUST carry ``from_status = NULL`` (no prior committed
    status) and ``attempt = 0`` (the QUEUED event precedes any attempt).
    """
    app = create_app()
    with TestClient(app) as client:
        body = _submit_job(client, "bm_event_submit")

    events = _events_for_job(db_session, body["id"])
    assert len(events) == 1
    event = events[0]
    assert event.kind == ConversionEventKind.QUEUED
    assert event.from_status is None
    assert event.to_status == ConversionJobStatus.QUEUED
    assert event.attempt == 0
    assert event.aizk_uuid is not None

    payload = json.loads(event.payload_json)
    assert payload["kind"] == "queued"
    assert payload["requeue_reason"] == "initial"
    assert payload["submitted_by"] == "self"


def test_retry_endpoint_emits_queued_event_with_retry_reason(db_session) -> None:
    """POST /v1/jobs/{id}/retry appends a `queued` event with retry semantics.

    The event's ``requeue_reason`` is ``"retry_endpoint"`` and ``attempt``
    equals the job's incremented attempt count.
    """
    app = create_app()
    with TestClient(app) as client:
        body = _submit_job(client, "bm_event_retry")
        job_id = body["id"]
        _force_failed_retryable(db_session, job_id)

        retry_resp = client.post(f"/v1/jobs/{job_id}/retry")
        assert retry_resp.status_code == 200, retry_resp.json()

    events = _events_for_job(db_session, job_id)
    # Origin queued + retry queued; no other API events fire.
    queued_events = [e for e in events if e.kind == ConversionEventKind.QUEUED]
    assert len(queued_events) == 2
    retry_event = queued_events[-1]

    job = db_session.get(ConversionJob, job_id)
    assert retry_event.attempt == job.attempts
    assert retry_event.attempt == 1
    assert retry_event.from_status == ConversionJobStatus.FAILED_RETRYABLE
    assert retry_event.to_status == ConversionJobStatus.QUEUED

    payload = json.loads(retry_event.payload_json)
    assert payload["requeue_reason"] == "retry_endpoint"
    assert payload["submitted_by"] == "self"


def test_cancel_endpoint_emits_cancelled_event(db_session) -> None:
    """POST /v1/jobs/{id}/cancel appends a `cancelled` event tagged with the caller."""
    app = create_app()
    with TestClient(app) as client:
        body = _submit_job(client, "bm_event_cancel")
        job_id = body["id"]

        cancel_resp = client.post(f"/v1/jobs/{job_id}/cancel")
        assert cancel_resp.status_code == 200, cancel_resp.json()

    events = _events_for_job(db_session, job_id)
    cancelled_events = [e for e in events if e.kind == ConversionEventKind.CANCELLED]
    assert len(cancelled_events) == 1
    cancel_event = cancelled_events[0]
    assert cancel_event.from_status == ConversionJobStatus.QUEUED
    assert cancel_event.to_status == ConversionJobStatus.CANCELLED

    payload = json.loads(cancel_event.payload_json)
    assert payload["kind"] == "cancelled"
    assert payload["cancelled_by"] == "self"


def test_bulk_retry_emits_one_queued_event_per_job(db_session) -> None:
    """POST /v1/jobs/actions with action=retry appends N `queued` events for N jobs."""
    app = create_app()
    with TestClient(app) as client:
        ids = [_submit_job(client, f"bm_event_bulk_retry_{i}")["id"] for i in range(3)]
        for jid in ids:
            _force_failed_retryable(db_session, jid)

        resp = client.post(
            "/v1/jobs/actions",
            json={"action": "retry", "job_ids": ids},
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert body["summary"]["success"] == 3
        assert body["summary"]["errors"] == 0

    for jid in ids:
        events = _events_for_job(db_session, jid)
        queued_events = [e for e in events if e.kind == ConversionEventKind.QUEUED]
        # origin queued + bulk-retry queued
        assert len(queued_events) == 2, f"job {jid} produced events {[e.kind for e in events]}"
        retry_event = queued_events[-1]
        assert retry_event.attempt == 1
        assert retry_event.from_status == ConversionJobStatus.FAILED_RETRYABLE
        payload = json.loads(retry_event.payload_json)
        assert payload["requeue_reason"] == "retry_endpoint"
        assert payload["submitted_by"] == "self"


def test_bulk_cancel_emits_one_cancelled_event_per_job(db_session) -> None:
    """POST /v1/jobs/actions with action=cancel appends N `cancelled` events for N jobs."""
    app = create_app()
    with TestClient(app) as client:
        ids = [_submit_job(client, f"bm_event_bulk_cancel_{i}")["id"] for i in range(3)]

        resp = client.post(
            "/v1/jobs/actions",
            json={"action": "cancel", "job_ids": ids},
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert body["summary"]["success"] == 3
        assert body["summary"]["errors"] == 0

    for jid in ids:
        events = _events_for_job(db_session, jid)
        cancelled_events = [e for e in events if e.kind == ConversionEventKind.CANCELLED]
        assert len(cancelled_events) == 1, f"job {jid} produced events {[e.kind for e in events]}"
        cancel_event = cancelled_events[0]
        assert cancel_event.from_status == ConversionJobStatus.QUEUED
        assert cancel_event.to_status == ConversionJobStatus.CANCELLED
        payload = json.loads(cancel_event.payload_json)
        assert payload["cancelled_by"] == "self"
