"""Integration tests for GET /v1/jobs list endpoint (filters + pagination)."""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.utilities.clock import utcnow
from tests.conversion._helpers import make_job, make_source


def _seed_retry_class_mix(db_session) -> list[int]:
    source = make_source(db_session, "bm_retry_class")
    make_job(db_session, source_id=source.source_id, idempotency_key="q" * 64, status=WorkUnitStatus.QUEUED)
    make_job(db_session, source_id=source.source_id, idempotency_key="p" * 64, status=WorkUnitStatus.FAILED)
    return [
        make_job(
            db_session,
            source_id=source.source_id,
            idempotency_key=key * 64,
            status=WorkUnitStatus.FAILED,
            earliest_next_attempt_at=utcnow(),
        ).id
        for key in ("a", "b")
    ]


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
    j_old = make_job(db_session, source_id=bookmark.source_id, idempotency_key="o" * 64, created_at=older)
    j_new = make_job(db_session, source_id=bookmark.source_id, idempotency_key="n" * 64, created_at=newer)

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
    make_job(db_session, source_id=bookmark.source_id, idempotency_key="q" * 64, status=WorkUnitStatus.QUEUED)
    j_succ = make_job(
        db_session, source_id=bookmark.source_id, idempotency_key="s" * 64, status=WorkUnitStatus.SUCCEEDED
    )

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs", params={"status": "succeeded"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [j["id"] for j in body["jobs"]] == [j_succ.id]


def test_list_filters_by_source_id(db_session) -> None:
    bookmark_a = make_source(db_session, "bm_uuid_a")
    bookmark_b = make_source(db_session, "bm_uuid_b")
    j_a = make_job(db_session, source_id=bookmark_a.source_id, idempotency_key="u" * 64)
    make_job(db_session, source_id=bookmark_b.source_id, idempotency_key="v" * 64)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs", params={"source_id": str(bookmark_a.source_id)})

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
            source_id=bookmark.source_id,
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


def test_list_filters_by_retry_class(db_session) -> None:
    """``retry_class=retryable`` returns only the failed jobs with a retry wait, and ``total`` counts only them."""
    retryable_ids = _seed_retry_class_mix(db_session)

    with TestClient(create_app()) as client:
        resp = client.get("/v1/jobs", params={"retry_class": "retryable"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert sorted(j["id"] for j in body["jobs"]) == sorted(retryable_ids)
    assert {j["retry_class"] for j in body["jobs"]} == {"retryable"}


def test_list_rejects_a_status_outside_the_lifecycle_vocabulary(db_session) -> None:
    """A status value that is not one of the six lifecycle values is rejected."""
    with TestClient(create_app()) as client:
        resp = client.get("/v1/jobs", params={"status": "FAILED_PERM"})

    assert resp.status_code == 422


def test_list_intersects_status_and_retry_class(db_session) -> None:
    """``status=queued&retry_class=retryable`` is an empty page with ``total: 0``."""
    _seed_retry_class_mix(db_session)

    with TestClient(create_app()) as client:
        resp = client.get("/v1/jobs", params={"status": "queued", "retry_class": "retryable"})

    assert resp.status_code == 200
    assert resp.json() == {"jobs": [], "total": 0, "limit": 50, "offset": 0}
