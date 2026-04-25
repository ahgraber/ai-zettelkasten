"""Integration tests for job status counts endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJobStatus
from tests.conversion._helpers import make_job, make_source


def test_status_counts_returns_totals(db_session) -> None:
    app = create_app()
    bookmark = make_source(db_session, "bm_status_counts")
    make_job(db_session, aizk_uuid=bookmark.aizk_uuid, status=ConversionJobStatus.QUEUED, idempotency_key="a" * 64)
    make_job(db_session, aizk_uuid=bookmark.aizk_uuid, status=ConversionJobStatus.QUEUED, idempotency_key="b" * 64)
    make_job(
        db_session,
        aizk_uuid=bookmark.aizk_uuid,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        idempotency_key="c" * 64,
    )

    with TestClient(app) as client:
        response = client.get("/v1/jobs/status-counts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["counts"] == {"QUEUED": 2, "FAILED_RETRYABLE": 1}


def test_status_counts_empty_returns_zero(db_session) -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/v1/jobs/status-counts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0
    assert payload["counts"] == {}
