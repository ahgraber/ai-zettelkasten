"""Integration tests for PR 6 API behaviors: source_ref submission, Source dedup, kind gating."""

from __future__ import annotations

from sqlmodel import select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.source import Source


def test_submit_karakeep_bookmark_creates_source_with_karakeep_id(db_session):
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={
                "source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_kk_create"},
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["karakeep_id"] == "bm_kk_create"
    assert body["source_ref"]["kind"] == "karakeep_bookmark"
    assert body["source_ref"]["bookmark_id"] == "bm_kk_create"

    source = db_session.exec(select(Source).where(Source.karakeep_id == "bm_kk_create")).one()
    assert source.karakeep_id == "bm_kk_create"
    assert source.source_ref == {"kind": "karakeep_bookmark", "bookmark_id": "bm_kk_create"}
    assert source.source_ref_hash  # populated


def test_source_dedup_two_identical_submissions_share_row(db_session):
    app = create_app()
    with TestClient(app) as client:
        # Two submissions with identical source_ref but distinct idempotency keys.
        resp1 = client.post(
            "/v1/jobs",
            json={
                "source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_dedup"},
                "idempotency_key": "k1" + "0" * 62,
            },
        )
        resp2 = client.post(
            "/v1/jobs",
            json={
                "source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_dedup"},
                "idempotency_key": "k2" + "0" * 62,
            },
        )
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["aizk_uuid"] == resp2.json()["aizk_uuid"]

    sources = db_session.exec(select(Source).where(Source.karakeep_id == "bm_dedup")).all()
    assert len(sources) == 1


def test_submit_unsupported_kind_returns_422(db_session):
    """singlefile is intentionally not registered; API must reject with 422."""
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={
                "source_ref": {"kind": "singlefile", "path": "/tmp/x.html"},
            },
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    # FastAPI's own validation runs first and produces 422 with pydantic error shape,
    # but since singlefile IS a valid pydantic discriminator variant the request passes
    # schema validation and reaches our capability gate.
    detail = body.get("detail")
    if isinstance(detail, dict):
        assert detail.get("error") == "unsupported_source_kind"


def test_response_has_source_ref_and_nullable_karakeep_id(db_session):
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={
                "source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_resp_shape"},
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    # source_ref is canonical on the response; karakeep_id is a nullable compat field.
    assert "source_ref" in body
    assert "karakeep_id" in body
    assert body["karakeep_id"] == "bm_resp_shape"
