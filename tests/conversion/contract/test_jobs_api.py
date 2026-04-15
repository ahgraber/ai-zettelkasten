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
        "/v1/jobs/actions": {"post"},
    }

    for path, methods in expected_paths.items():
        assert path in app_spec["paths"], f"Missing route: {path}"
        for method in methods:
            assert method in app_spec["paths"][path], f"Missing method {method} on {path}"


def test_jobs_schemas_registered():
    app_spec = create_app().openapi()

    if not app_spec.get("components"):
        pytest.xfail("API schemas not registered in FastAPI app yet.")

    for schema_name in (
        "JobSubmission",
        "JobResponse",
        "JobList",
        "JobStatusCounts",
        "ConversionJobStatus",
        "BulkJobActionRequest",
        "BulkActionResponse",
    ):
        assert schema_name in app_spec["components"]["schemas"], f"Missing schema: {schema_name}"


def test_list_jobs_query_params_declared():
    """GET /v1/jobs must expose all documented filters and pagination params."""
    app_spec = create_app().openapi()

    if not app_spec.get("paths"):
        pytest.xfail("API routes not registered in FastAPI app yet.")

    get_op = app_spec["paths"]["/v1/jobs"]["get"]
    declared = {p["name"] for p in get_op.get("parameters", [])}
    expected = {"status", "aizk_uuid", "karakeep_id", "created_after", "created_before", "limit", "offset"}
    assert expected.issubset(declared), f"Missing query params: {expected - declared}"


def test_bulk_actions_request_schema_shape():
    """POST /v1/jobs/actions request body must declare action + job_ids fields."""
    app_spec = create_app().openapi()

    if not app_spec.get("components"):
        pytest.xfail("API schemas not registered in FastAPI app yet.")

    schema = app_spec["components"]["schemas"]["BulkJobActionRequest"]
    assert set(schema["properties"].keys()) >= {"action", "job_ids"}
    assert set(schema.get("required", [])) >= {"action", "job_ids"}
