"""Integration tests for GET /v1/jobs list endpoint (filters + pagination)."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as Bookmark


def _create_bookmark(session, karakeep_id: str) -> Bookmark:
    bookmark = Bookmark(
        karakeep_id=karakeep_id,
        url=f"https://example.com/{karakeep_id}",
        normalized_url=f"https://example.com/{karakeep_id}",
        title=karakeep_id,
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
    idempotency_key: str,
    status: ConversionJobStatus = ConversionJobStatus.QUEUED,
    created_at: dt.datetime | None = None,
) -> ConversionJob:
    job = ConversionJob(
        aizk_uuid=aizk_uuid,
        title="T",
        payload_version=1,
        status=status,
        attempts=0,
        idempotency_key=idempotency_key,
    )
    if created_at is not None:
        job.created_at = created_at
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_list_empty_returns_zero_total(db_session) -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"jobs": [], "total": 0, "limit": 50, "offset": 0}


def test_list_returns_jobs_ordered_descending_by_created_at(db_session) -> None:
    bookmark = _create_bookmark(db_session, "bm_list")
    older = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    j_old = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="o" * 64, created_at=older)
    j_new = _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="n" * 64, created_at=newer)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    ids_in_order = [j["id"] for j in body["jobs"]]
    assert ids_in_order == [j_new.id, j_old.id]


def test_list_filters_by_status(db_session) -> None:
    bookmark = _create_bookmark(db_session, "bm_status")
    _create_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="q" * 64, status=ConversionJobStatus.QUEUED)
    j_succ = _create_job(
        db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="s" * 64, status=ConversionJobStatus.SUCCEEDED
    )

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs", params={"status": "SUCCEEDED"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [j["id"] for j in body["jobs"]] == [j_succ.id]


def test_list_filters_by_aizk_uuid(db_session) -> None:
    bookmark_a = _create_bookmark(db_session, "bm_uuid_a")
    bookmark_b = _create_bookmark(db_session, "bm_uuid_b")
    j_a = _create_job(db_session, aizk_uuid=bookmark_a.aizk_uuid, idempotency_key="u" * 64)
    _create_job(db_session, aizk_uuid=bookmark_b.aizk_uuid, idempotency_key="v" * 64)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs", params={"aizk_uuid": str(bookmark_a.aizk_uuid)})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [j["id"] for j in body["jobs"]] == [j_a.id]


def test_list_paginates_with_limit_and_offset(db_session) -> None:
    bookmark = _create_bookmark(db_session, "bm_page")
    # Seed 5 jobs with distinct created_at so ordering is deterministic.
    base = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
    ids_newest_first = []
    for i in range(5):
        j = _create_job(
            db_session,
            aizk_uuid=bookmark.aizk_uuid,
            idempotency_key=f"p{i}".ljust(64, "0"),
            created_at=base + dt.timedelta(hours=i),
        )
        ids_newest_first.append(j.id)
    ids_newest_first.reverse()

    app = create_app()
    with TestClient(app) as client:
        page1 = client.get("/v1/jobs", params={"limit": 2, "offset": 0}).json()
        page2 = client.get("/v1/jobs", params={"limit": 2, "offset": 2}).json()

    assert page1["total"] == 5
    assert page1["limit"] == 2
    assert page1["offset"] == 0
    assert [j["id"] for j in page1["jobs"]] == ids_newest_first[:2]
    assert [j["id"] for j in page2["jobs"]] == ids_newest_first[2:4]


def test_list_rejects_invalid_pagination(db_session) -> None:
    app = create_app()
    with TestClient(app) as client:
        resp_neg = client.get("/v1/jobs", params={"offset": -1})
        resp_over = client.get("/v1/jobs", params={"limit": 10000})
        resp_zero = client.get("/v1/jobs", params={"limit": 0})

    assert resp_neg.status_code == 422
    assert resp_over.status_code == 422
    assert resp_zero.status_code == 422
