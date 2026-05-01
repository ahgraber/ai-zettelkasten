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


def test_cross_owner_same_key_creates_distinct_jobs_sharing_a_source(db_session) -> None:
    """Different principals with the same computed key get their own Job and share a Source row.

    Simulates a second principal by inserting an existing job with `owner_id != "self"`
    that holds the key the first /v1/jobs request would compute. The API request runs
    as principal "self" and must not match that pre-existing job; it must instead
    create its own job while reusing the existing Source row keyed by source_ref_hash.
    """
    from sqlmodel import select

    from aizk.conversion.datamodel.job import ConversionJob
    from aizk.conversion.datamodel.source import Source
    from aizk.conversion.utilities.hashing import compute_idempotency_key
    from tests.conversion._helpers import make_job, make_source

    bookmark_id = "bm_xowner_idem"
    bookmark = make_source(db_session, bookmark_id, owner_id="someone_else")
    # Pre-compute the key the API will compute for the same source/config.
    app = create_app()
    with TestClient(app) as client:
        # Compute the converter snapshot the way the API does — use a direct call after
        # building the app so app.state is populated.
        snapshot = app.state.converter_config_snapshot
        converter_name = app.state.converter_name
        from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash

        ref = KarakeepBookmarkRef(bookmark_id=bookmark_id)
        computed_key = compute_idempotency_key(compute_source_ref_hash(ref), converter_name, snapshot)
        cross_job = make_job(
            db_session,
            aizk_uuid=bookmark.aizk_uuid,
            idempotency_key=computed_key,
            owner_id="someone_else",
        )

        resp = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}},
        )

    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert body["id"] != cross_job.id
    assert body["idempotency_key"] == computed_key

    # Source row reused (single row keyed by source_ref_hash).
    sources = db_session.exec(select(Source).where(Source.karakeep_id == bookmark_id)).all()
    assert len(sources) == 1

    # Two distinct jobs, one per owner, hold the same idempotency_key.
    jobs = db_session.exec(select(ConversionJob).where(ConversionJob.idempotency_key == computed_key)).all()
    owners = sorted(j.owner_id for j in jobs)
    assert owners == ["self", "someone_else"]


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
