"""Contract tests for health check endpoints."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app


@pytest.fixture()
def client() -> TestClient:
    """Create a TestClient with full lifespan (conftest provides test DB env)."""
    app = create_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture()
def _mock_s3_healthy():
    """Mock S3 head_bucket to succeed."""
    with patch("aizk.conversion.api.routes.health.S3Client") as mock_cls:
        instance = MagicMock()
        instance.client.head_bucket.return_value = {}
        instance.config.s3_bucket_name = "test-bucket"
        mock_cls.return_value = instance
        yield


@pytest.fixture()
def _mock_s3_unavailable():
    """Mock S3 head_bucket to raise an error."""
    with patch("aizk.conversion.api.routes.health.S3Client") as mock_cls:
        instance = MagicMock()
        instance.client.head_bucket.side_effect = Exception("connection refused")
        instance.config.s3_bucket_name = "test-bucket"
        mock_cls.return_value = instance
        yield


@pytest.fixture()
def _mock_s3_slow():
    """Mock S3 head_bucket to block longer than the check timeout."""
    with patch("aizk.conversion.api.routes.health.S3Client") as mock_cls:
        instance = MagicMock()
        instance.client.head_bucket.side_effect = lambda **kwargs: time.sleep(10)
        instance.config.s3_bucket_name = "test-bucket"
        mock_cls.return_value = instance
        yield


@pytest.fixture()
def _mock_db_unavailable():
    """Mock DB engine to raise on connect."""
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("database is locked")

    with patch("aizk.conversion.api.routes.health.get_engine", return_value=mock_engine):
        yield


class TestLiveness:
    def test_returns_200_with_status_ok(self, client):
        resp = client.get("/health/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"] == []


class TestReadiness:
    @pytest.mark.usefixtures("_mock_s3_healthy")
    def test_all_healthy_returns_200(self, client):
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        checks_by_name = {c["name"]: c for c in body["checks"]}
        assert checks_by_name["database"]["status"] == "ok"
        assert checks_by_name["s3"]["status"] == "ok"

    @pytest.mark.usefixtures("_mock_s3_healthy", "_mock_db_unavailable")
    def test_db_unreachable_returns_503(self, client):
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        checks_by_name = {c["name"]: c for c in body["checks"]}
        assert checks_by_name["database"]["status"] == "unavailable"
        assert "database is locked" in checks_by_name["database"]["detail"]

    @pytest.mark.usefixtures("_mock_s3_unavailable")
    def test_s3_unreachable_returns_503(self, client):
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        checks_by_name = {c["name"]: c for c in body["checks"]}
        assert checks_by_name["s3"]["status"] == "unavailable"
        assert "connection refused" in checks_by_name["s3"]["detail"]

    @pytest.mark.usefixtures("_mock_s3_unavailable", "_mock_db_unavailable")
    def test_both_unreachable_returns_503_with_both_failures(self, client):
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        checks_by_name = {c["name"]: c for c in body["checks"]}
        assert checks_by_name["database"]["status"] == "unavailable"
        assert checks_by_name["s3"]["status"] == "unavailable"

    @pytest.mark.usefixtures("_mock_s3_slow")
    def test_timeout_enforced(self, client):
        with patch("aizk.conversion.api.routes.health._CHECK_TIMEOUT_SECONDS", 0.1):
            resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        checks_by_name = {c["name"]: c for c in body["checks"]}
        assert checks_by_name["s3"]["status"] == "unavailable"
        assert checks_by_name["s3"]["detail"] == "timeout"
