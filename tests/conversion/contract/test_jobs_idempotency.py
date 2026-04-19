"""Contract tests for POST /v1/jobs idempotency semantics.

Pins the spec contract: first submission of an idempotency key returns 201
Created; subsequent submissions of the same key return 200 OK with the
original job's payload.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app


def test_first_submission_returns_201(db_session) -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={
                "source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_idem_first"},
                "idempotency_key": "first-key".ljust(64, "0"),
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["idempotency_key"] == "first-key".ljust(64, "0")
    assert body["status"] == "QUEUED"


def test_duplicate_submission_returns_200_with_original_payload(db_session) -> None:
    app = create_app()
    key = "dup-key".ljust(64, "0")
    with TestClient(app) as client:
        first = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_idem_dup"}, "idempotency_key": key},
        )
        second = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_idem_dup"}, "idempotency_key": key},
        )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["idempotency_key"] == key


def test_distinct_keys_both_return_201(db_session) -> None:
    app = create_app()
    with TestClient(app) as client:
        a = client.post(
            "/v1/jobs",
            json={
                "source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_idem_distinct"},
                "idempotency_key": "key-a".ljust(64, "0"),
            },
        )
        b = client.post(
            "/v1/jobs",
            json={
                "source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_idem_distinct"},
                "idempotency_key": "key-b".ljust(64, "0"),
            },
        )

    assert a.status_code == 201
    assert b.status_code == 201
    assert a.json()["id"] != b.json()["id"]
