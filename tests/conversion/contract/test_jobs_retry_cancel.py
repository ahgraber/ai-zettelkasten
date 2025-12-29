"""Contract tests for retry and cancel job endpoints."""

import pytest

from aizk.conversion.api.main import create_app


def test_job_retry_cancel_endpoints_registered() -> None:
    app_spec = create_app().openapi()

    if not app_spec.get("paths"):
        pytest.xfail("API routes not registered in FastAPI app yet.")

    expected_paths = {
        "/v1/jobs/{job_id}/retry": {"post"},
        "/v1/jobs/{job_id}/cancel": {"post"},
    }

    for path, methods in expected_paths.items():
        assert path in app_spec["paths"], f"Missing route: {path}"
        for method in methods:
            assert method in app_spec["paths"][path], f"Missing method {method} on {path}"
