"""Integration tests for job bulk actions."""

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus


def _create_bookmark(session, karakeep_id: str, url: str, title: str) -> Bookmark:
    bookmark = Bookmark(
        karakeep_id=karakeep_id,
        url=url,
        normalized_url=url,
        title=title,
        content_type="html",
        source_type="other",
    )
    session.add(bookmark)
    session.commit()
    session.refresh(bookmark)
    return bookmark


def _create_job(
    session,
    *,
    aizk_uuid: UUID,
    title: str,
    status: ConversionJobStatus,
    idempotency_key: str,
    attempts: int = 0,
) -> ConversionJob:
    job = ConversionJob(
        aizk_uuid=aizk_uuid,
        title=title,
        payload_version=1,
        status=status,
        attempts=attempts,
        idempotency_key=idempotency_key,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_bulk_retry_resets_failed_jobs(db_session) -> None:
    app = create_app()
    bookmark = _create_bookmark(db_session, "bm_bulk_retry", "https://example.com/retry", "Retry Example")
    job_retryable = _create_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        idempotency_key="a" * 64,
        attempts=1,
    )
    job_cancelled = _create_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
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


def test_bulk_cancel_marks_queued_and_running_jobs(db_session) -> None:
    app = create_app()
    bookmark = _create_bookmark(db_session, "bm_bulk_cancel", "https://example.com/cancel", "Cancel Example")
    job_queued = _create_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        status=ConversionJobStatus.QUEUED,
        idempotency_key="c" * 64,
    )
    job_running = _create_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
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
