"""Integration tests for queue backpressure on job submission."""

from __future__ import annotations

from uuid import UUID

from sqlmodel import select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as Bookmark


def _create_bookmark(session, karakeep_id: str) -> Bookmark:
    bookmark = Bookmark(
        karakeep_id=karakeep_id,
        url="https://example.com",
        normalized_url="https://example.com",
        title="Test",
        content_type="html",
        source_type="other",
    )
    session.add(bookmark)
    session.commit()
    session.refresh(bookmark)
    return bookmark


def _create_queued_job(session, *, aizk_uuid: UUID, idempotency_key: str) -> ConversionJob:
    job = ConversionJob(
        aizk_uuid=aizk_uuid,
        title="Test",
        payload_version=1,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key=idempotency_key,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _fill_queue(session, bookmark: Bookmark, count: int) -> list[ConversionJob]:
    """Create `count` QUEUED jobs for the given bookmark."""
    jobs = []
    for i in range(count):
        jobs.append(
            _create_queued_job(
                session,
                aizk_uuid=bookmark.aizk_uuid,
                idempotency_key=f"fill-{i:04d}",
            )
        )
    return jobs


def test_submit_rejected_when_queue_at_capacity(db_session, monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_MAX_DEPTH", "3")
    app = create_app()
    bookmark = _create_bookmark(db_session, "bp-reject")
    _fill_queue(db_session, bookmark, 3)

    with TestClient(app) as client:
        response = client.post("/v1/jobs", json={"karakeep_id": "bp-new-job"})

    assert response.status_code == 503
    body = response.json()
    assert "capacity" in body["detail"].lower()
    # Response body must match QueueFullResponse schema (detail + retry_after).
    assert "retry_after" in body
    assert isinstance(body["retry_after"], int)


def test_rejected_submission_does_not_create_orphan_bookmark(db_session, monkeypatch) -> None:
    """A queue-full rejection must not leave a bookmark row for the new karakeep_id."""
    monkeypatch.setenv("QUEUE_MAX_DEPTH", "1")
    app = create_app()
    existing_bm = _create_bookmark(db_session, "bp-orphan-fill")
    _fill_queue(db_session, existing_bm, 1)

    with TestClient(app) as client:
        response = client.post("/v1/jobs", json={"karakeep_id": "bp-orphan-new"})

    assert response.status_code == 503
    orphan = db_session.exec(select(Bookmark).where(Bookmark.karakeep_id == "bp-orphan-new")).first()
    assert orphan is None, "Rejected submission should not persist a bookmark row"


def test_submit_accepted_when_queue_below_capacity(db_session, monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_MAX_DEPTH", "5")
    app = create_app()
    bookmark = _create_bookmark(db_session, "bp-accept")
    _fill_queue(db_session, bookmark, 2)

    with TestClient(app) as client:
        response = client.post("/v1/jobs", json={"karakeep_id": "bp-new-accept"})

    assert response.status_code == 201


def test_duplicate_bypasses_queue_depth_check(db_session, monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_MAX_DEPTH", "2")
    app = create_app()
    bookmark = _create_bookmark(db_session, "bp-dup")
    _fill_queue(db_session, bookmark, 2)

    # Submit a job that will become a duplicate
    existing = _create_queued_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="dup-key-00",
    )

    with TestClient(app) as client:
        # Resubmit with the same idempotency key — should return 200, not 503
        response = client.post(
            "/v1/jobs",
            json={"karakeep_id": "bp-dup", "idempotency_key": "dup-key-00"},
        )

    assert response.status_code == 200
    assert response.json()["id"] == existing.id


def test_retry_after_header_present_on_503(db_session, monkeypatch) -> None:
    monkeypatch.setenv("QUEUE_MAX_DEPTH", "1")
    monkeypatch.setenv("QUEUE_RETRY_AFTER_SECONDS", "45")
    app = create_app()
    bookmark = _create_bookmark(db_session, "bp-header")
    _fill_queue(db_session, bookmark, 1)

    with TestClient(app) as client:
        response = client.post("/v1/jobs", json={"karakeep_id": "bp-header-new"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "45"
