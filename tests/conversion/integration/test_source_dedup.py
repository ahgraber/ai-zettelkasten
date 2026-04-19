"""Integration tests for Source materialization, dedup, and ingress kind gating."""

from __future__ import annotations

import threading

from sqlmodel import select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.source import Source


def _submit(client, bookmark_id: str, idempotency_key=None) -> dict:
    body = {"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}}
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    resp = client.post("/v1/jobs", json=body)
    return resp


def test_source_created_on_karakeep_submission(db_session) -> None:
    """POST with KarakeepBookmarkRef creates Source row with karakeep_id populated."""
    app = create_app()
    with TestClient(app) as client:
        resp = _submit(client, "bm_new")
    assert resp.status_code == 201
    source = db_session.exec(select(Source).where(Source.karakeep_id == "bm_new")).one()
    assert source.karakeep_id == "bm_new"
    assert source.source_ref is not None
    assert source.source_ref_hash is not None


def test_source_dedup_same_bookmark_id(db_session) -> None:
    """Two submissions with same bookmark_id share one Source row."""
    app = create_app()
    with TestClient(app) as client:
        resp1 = _submit(client, "bm_dedup")
        resp2 = _submit(client, "bm_dedup")
    # Both must succeed
    assert resp1.status_code == 201
    assert resp2.status_code in (200, 201)
    sources = db_session.exec(select(Source).where(Source.karakeep_id == "bm_dedup")).all()
    assert len(sources) == 1
    # Both jobs reference the same aizk_uuid
    assert resp1.json()["aizk_uuid"] == resp2.json()["aizk_uuid"]


def test_source_response_includes_source_ref(db_session) -> None:
    """JobResponse includes source_ref with correct kind and bookmark_id."""
    app = create_app()
    with TestClient(app) as client:
        resp = _submit(client, "bm_resp_check")
    assert resp.status_code == 201
    body = resp.json()
    assert "source_ref" in body
    assert body["source_ref"]["kind"] == "karakeep_bookmark"
    assert body["source_ref"]["bookmark_id"] == "bm_resp_check"
    assert body["karakeep_id"] == "bm_resp_check"


def test_idempotency_key_stable_for_same_source_ref(db_session) -> None:
    """Same source_ref → same idempotency key → second submission returns 200."""
    app = create_app()
    with TestClient(app) as client:
        first = _submit(client, "bm_idem_stable")
        second = _submit(client, "bm_idem_stable")
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_idempotency_key_differs_for_different_source_ref(db_session) -> None:
    """Different bookmark_ids → different idempotency keys → two distinct jobs."""
    app = create_app()
    with TestClient(app) as client:
        first = _submit(client, "bm_diff_a")
        second = _submit(client, "bm_diff_b")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["idempotency_key"] != second.json()["idempotency_key"]


def test_concurrent_source_dedup_single_row(db_session) -> None:
    """Two threads POSTing the same bookmark_id concurrently produce exactly one Source row.

    Both requests must succeed (201 or 200) and reference the same aizk_uuid.
    A Barrier synchronises thread dispatch to maximise the chance of a genuine
    concurrent INSERT OR IGNORE collision.

    A single TestClient is shared across threads (thread-safe for request
    dispatch) so both requests share one app startup and one SQLite file,
    avoiding deadlock from two independent BEGIN IMMEDIATE transactions in
    separate lifespan contexts.
    """
    app = create_app()
    responses: list = [None, None]
    barrier = threading.Barrier(2)

    def submit_thread(index: int, client: TestClient) -> None:
        barrier.wait()  # release both threads at the same moment
        responses[index] = _submit(client, "bm_concurrent_dedup")

    with TestClient(app) as client:
        threads = [threading.Thread(target=submit_thread, args=(i, client)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert responses[0] is not None
    assert responses[1] is not None
    assert responses[0].status_code in (200, 201)
    assert responses[1].status_code in (200, 201)

    # Both jobs must reference the same source UUID
    assert responses[0].json()["aizk_uuid"] == responses[1].json()["aizk_uuid"]

    # Exactly one Source row exists for this bookmark_id
    sources = db_session.exec(select(Source).where(Source.karakeep_id == "bm_concurrent_dedup")).all()
    assert len(sources) == 1
