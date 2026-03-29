"""Unit tests for health check response schemas."""

import pytest

from aizk.conversion.api.schemas.health import CheckResult, HealthResponse


class TestCheckResult:
    def test_ok_check_has_null_detail(self):
        result = CheckResult(name="database", status="ok")
        assert result.detail is None

    def test_unavailable_check_includes_detail(self):
        result = CheckResult(name="s3", status="unavailable", detail="timeout")
        assert result.status == "unavailable"
        assert result.detail == "timeout"

    @pytest.mark.parametrize("status", ["ok", "unavailable"])
    def test_serialization_roundtrip(self, status):
        result = CheckResult(name="test", status=status, detail="info" if status == "unavailable" else None)
        data = result.model_dump()
        restored = CheckResult.model_validate(data)
        assert restored == result


class TestHealthResponse:
    def test_ok_response_with_empty_checks(self):
        resp = HealthResponse(status="ok")
        assert resp.status == "ok"
        assert resp.checks == []

    def test_ok_response_with_passing_checks(self):
        checks = [
            CheckResult(name="database", status="ok"),
            CheckResult(name="s3", status="ok"),
        ]
        resp = HealthResponse(status="ok", checks=checks)
        data = resp.model_dump()
        assert data["status"] == "ok"
        assert len(data["checks"]) == 2
        assert all(c["status"] == "ok" for c in data["checks"])

    def test_unavailable_response_with_failing_checks(self):
        checks = [
            CheckResult(name="database", status="unavailable", detail="connection refused"),
            CheckResult(name="s3", status="ok"),
        ]
        resp = HealthResponse(status="unavailable", checks=checks)
        data = resp.model_dump()
        assert data["status"] == "unavailable"
        assert data["checks"][0]["detail"] == "connection refused"
        assert data["checks"][1]["detail"] is None
