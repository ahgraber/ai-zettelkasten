"""Integration tests for IngressSourceRef kind gating at the API boundary."""

from __future__ import annotations

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app


def test_submit_karakeep_bookmark_accepted(db_session) -> None:
    """POST /v1/jobs with karakeep_bookmark kind returns 201."""
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "kg-accepted"}},
        )
    assert resp.status_code == 201


def test_submit_url_kind_rejected_by_policy(db_session) -> None:
    """POST /v1/jobs with url kind returns 422.

    The url kind is not in IngressSourceRef (narrow union), so Pydantic's
    discriminator rejects it at schema-validation time — before the policy gate.
    """
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "url", "url": "https://example.com"}},
        )
    assert resp.status_code == 422


def test_submit_singlefile_kind_rejected_by_schema(db_session) -> None:
    """POST /v1/jobs with singlefile kind returns 422.

    The singlefile kind is not present in IngressSourceRef at all (narrow union).
    Pydantic's discriminator rejects it at schema-validation time.
    """
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "singlefile", "url": "file:///tmp/foo.pdf"}},
        )
    assert resp.status_code == 422


def test_submit_karakeep_bookmark_with_whitespace_rejected_by_schema(db_session) -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": " bad id"}},
        )
    assert resp.status_code == 422
