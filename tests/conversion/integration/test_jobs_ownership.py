"""Integration tests for ownership-scoped job visibility.

Every request in `trust_network` mode resolves to `Principal(subject="self")`
(the shipped `AIZK_DEFAULT_PRINCIPAL`). These tests seed jobs with `owner_id`
values matching and not matching that subject and assert that read and mutation
routes filter / reject by ownership.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJobStatus
from tests.conversion._helpers import make_job, make_source


def test_list_excludes_cross_owner_jobs(db_session) -> None:
    bookmark = make_source(db_session, "bm_list_owner")
    owned = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="o" * 64,
        owner_id="self",
    )
    make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="x" * 64,
        owner_id="someone_else",
    )

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [j["id"] for j in body["jobs"]] == [owned.id]


def test_get_returns_404_for_cross_owner_job(db_session) -> None:
    bookmark = make_source(db_session, "bm_get_owner")
    owned = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="g" * 64,
        owner_id="self",
    )
    cross = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="h" * 64,
        owner_id="someone_else",
    )

    app = create_app()
    with TestClient(app) as client:
        owned_resp = client.get(f"/v1/jobs/{owned.id}")
        cross_resp = client.get(f"/v1/jobs/{cross.id}")

    assert owned_resp.status_code == 200
    assert owned_resp.json()["id"] == owned.id

    assert cross_resp.status_code == 404
    assert cross_resp.json()["detail"]["error"] == "job_not_found"


def test_status_counts_excludes_cross_owner_jobs(db_session) -> None:
    bookmark = make_source(db_session, "bm_counts_owner")
    make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="a" * 64,
        status=ConversionJobStatus.QUEUED,
        owner_id="self",
    )
    make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="b" * 64,
        status=ConversionJobStatus.QUEUED,
        owner_id="someone_else",
    )
    make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="c" * 64,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        owner_id="someone_else",
    )

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/v1/jobs/status-counts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["counts"] == {"QUEUED": 1}


def test_retry_returns_404_for_cross_owner_job(db_session) -> None:
    bookmark = make_source(db_session, "bm_retry_owner")
    cross = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="r" * 64,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        owner_id="someone_else",
    )

    app = create_app()
    with TestClient(app) as client:
        resp = client.post(f"/v1/jobs/{cross.id}/retry")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "job_not_found"

    db_session.refresh(cross)
    assert cross.status == ConversionJobStatus.FAILED_RETRYABLE


def test_cancel_returns_404_for_cross_owner_job(db_session) -> None:
    bookmark = make_source(db_session, "bm_cancel_owner")
    cross = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="z" * 64,
        status=ConversionJobStatus.QUEUED,
        owner_id="someone_else",
    )

    app = create_app()
    with TestClient(app) as client:
        resp = client.post(f"/v1/jobs/{cross.id}/cancel")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "job_not_found"

    db_session.refresh(cross)
    assert cross.status == ConversionJobStatus.QUEUED


def test_bulk_action_skips_cross_owner_jobs(db_session) -> None:
    bookmark = make_source(db_session, "bm_bulk_owner")
    owned = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="m" * 64,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        owner_id="self",
    )
    cross = make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        idempotency_key="n" * 64,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        owner_id="someone_else",
    )

    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs/actions",
            json={"action": "retry", "job_ids": [owned.id, cross.id]},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"] == {"success": 1, "errors": 1}

    by_id = {item["job_id"]: item for item in payload["results"]}
    assert by_id[owned.id]["status"] == "success"
    assert by_id[cross.id]["status"] == "error"
    assert by_id[cross.id]["error"] == "job_not_found"

    db_session.refresh(owned)
    db_session.refresh(cross)
    assert owned.status == ConversionJobStatus.QUEUED
    assert cross.status == ConversionJobStatus.FAILED_RETRYABLE
