"""Integration tests for job bulk actions."""

from __future__ import annotations

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJobStatus
from tests.conversion._helpers import make_job, make_source


def test_bulk_retry_resets_failed_jobs(db_session) -> None:
    app = create_app()
    bookmark = make_source(db_session, "bm_bulk_retry")
    job_retryable = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        idempotency_key="a" * 64,
        attempts=1,
    )
    job_cancelled = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        status=ConversionJobStatus.CANCELLED,
        idempotency_key="b" * 64,
        attempts=2,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs/actions",
            json={"action": "retry", "job_ids": [job_retryable.id, job_cancelled.id]},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "retry"
    assert payload["summary"]["success"] == 2
    assert payload["summary"]["errors"] == 0

    result_statuses = {item["job_id"]: item["status"] for item in payload["results"]}
    assert result_statuses[job_retryable.id] == "success"
    assert result_statuses[job_cancelled.id] == "success"

    db_session.refresh(job_retryable)
    db_session.refresh(job_cancelled)
    assert job_retryable.status == ConversionJobStatus.QUEUED
    assert job_retryable.attempts == 2
    assert job_cancelled.status == ConversionJobStatus.QUEUED
    assert job_cancelled.attempts == 3


def test_single_retry_increments_attempt_count(db_session) -> None:
    app = create_app()
    bookmark = make_source(db_session, "bm_single_retry")
    job = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        idempotency_key="e" * 64,
        attempts=3,
    )

    with TestClient(app) as client:
        response = client.post(f"/v1/jobs/{job.id}/retry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "QUEUED"
    assert payload["attempts"] == 4

    db_session.refresh(job)
    assert job.attempts == 4


def test_bulk_cancel_marks_queued_and_running_jobs(db_session) -> None:
    app = create_app()
    bookmark = make_source(db_session, "bm_bulk_cancel")
    job_queued = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        status=ConversionJobStatus.QUEUED,
        idempotency_key="c" * 64,
    )
    job_running = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        status=ConversionJobStatus.RUNNING,
        idempotency_key="d" * 64,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs/actions",
            json={"action": "cancel", "job_ids": [job_queued.id, job_running.id]},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "cancel"
    assert payload["summary"]["success"] == 2
    assert payload["summary"]["errors"] == 0

    result_statuses = {item["job_id"]: item["status"] for item in payload["results"]}
    assert result_statuses[job_queued.id] == "success"
    assert result_statuses[job_running.id] == "success"

    db_session.refresh(job_queued)
    db_session.refresh(job_running)
    assert job_queued.status == ConversionJobStatus.CANCELLED
    assert job_running.status == ConversionJobStatus.CANCELLED
