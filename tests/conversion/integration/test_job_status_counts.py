"""Integration tests for job status counts endpoint."""

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as Bookmark


def _create_bookmark(db_session, karakeep_id: str) -> Bookmark:
    bookmark = Bookmark(
        karakeep_id=karakeep_id,
        aizk_uuid=UUID("550e8400-e29b-41d4-a716-446655440000"),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Status Count Example",
        content_type="html",
        source_type="other",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)
    return bookmark


def _create_job(db_session, *, aizk_uuid: UUID, status: ConversionJobStatus, idempotency_key: str) -> ConversionJob:
    job = ConversionJob(
        aizk_uuid=aizk_uuid,
        title="Status Count Job",
        payload_version=1,
        status=status,
        attempts=0,
        idempotency_key=idempotency_key,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def test_status_counts_returns_totals(db_session) -> None:
    app = create_app()
    bookmark = _create_bookmark(db_session, "bm_status_counts")
    _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, status=ConversionJobStatus.QUEUED, idempotency_key="a" * 64)
    _create_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        status=ConversionJobStatus.QUEUED,
        idempotency_key="b" * 64,
    )
    _create_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        idempotency_key="c" * 64,
    )

    with TestClient(app) as client:
        response = client.get("/v1/jobs/status-counts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["counts"] == {"QUEUED": 2, "FAILED_RETRYABLE": 1}


def test_status_counts_empty_returns_zero(db_session) -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/v1/jobs/status-counts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0
    assert payload["counts"] == {}
