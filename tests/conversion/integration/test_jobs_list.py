"""Integration tests for GET /v1/jobs list endpoint (filters + pagination)."""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJobStatus
from tests.conversion._helpers import make_job, make_source


def test_list_empty_returns_zero_total(db_session) -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"jobs": [], "total": 0, "limit": 50, "offset": 0}


def test_list_returns_jobs_ordered_descending_by_created_at(db_session) -> None:
    bookmark = make_source(db_session, "bm_list")
    older = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    j_old = make_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="o" * 64, created_at=older)
    j_new = make_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="n" * 64, created_at=newer)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    ids_in_order = [j["id"] for j in body["jobs"]]
    assert ids_in_order == [j_new.id, j_old.id]


def test_list_filters_by_status(db_session) -> None:
    bookmark = make_source(db_session, "bm_status")
    make_job(db_session, aizk_uuid=bookmark.aizk_uuid, idempotency_key="q" * 64, status=ConversionJobStatus.QUEUED)
    j_succ = make_job(
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
    bookmark_a = make_source(db_session, "bm_uuid_a")
    bookmark_b = make_source(db_session, "bm_uuid_b")
    j_a = make_job(db_session, aizk_uuid=bookmark_a.aizk_uuid, idempotency_key="u" * 64)
    make_job(db_session, aizk_uuid=bookmark_b.aizk_uuid, idempotency_key="v" * 64)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs", params={"aizk_uuid": str(bookmark_a.aizk_uuid)})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [j["id"] for j in body["jobs"]] == [j_a.id]


def test_list_paginates_with_limit_and_offset(db_session) -> None:
    bookmark = make_source(db_session, "bm_page")
    # Seed 5 jobs with distinct created_at so ordering is deterministic.
    base = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
    ids_newest_first = []
    for i in range(5):
        j = make_job(
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
