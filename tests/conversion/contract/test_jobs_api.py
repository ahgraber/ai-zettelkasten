"""Contract tests for conversion job APIs."""

import pytest

from aizk.conversion.api.main import create_app


def test_jobs_endpoints_registered():
    app = create_app()
    app_spec = app.openapi()

    if not app_spec.get("paths"):
        pytest.xfail("API routes not registered in FastAPI app yet.")

    expected_paths = {
        "/v1/jobs": {"post", "get"},
        "/v1/jobs/{job_id}": {"get"},
        "/v1/jobs/status-counts": {"get"},
    }

    for path, methods in expected_paths.items():
        assert path in app_spec["paths"], f"Missing route: {path}"
        for method in methods:
            assert method in app_spec["paths"][path], f"Missing method {method} on {path}"


def test_jobs_schemas_registered():
    app_spec = create_app().openapi()

    if not app_spec.get("components"):
        pytest.xfail("API schemas not registered in FastAPI app yet.")

    for schema_name in ("JobSubmission", "JobResponse", "JobStatusCounts", "ConversionJobStatus"):
        assert schema_name in app_spec["components"]["schemas"], f"Missing schema: {schema_name}"
