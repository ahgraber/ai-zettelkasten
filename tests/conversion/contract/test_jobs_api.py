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
        "KarakeepBookmarkRef",
    ):
        assert schema_name in app_spec["components"]["schemas"], f"Missing schema: {schema_name}"


def test_list_jobs_query_params_declared():
    """GET /v1/jobs must expose all documented filters and pagination params."""
    app_spec = create_app().openapi()

    if not app_spec.get("paths"):
        pytest.xfail("API routes not registered in FastAPI app yet.")

    get_op = app_spec["paths"]["/v1/jobs"]["get"]
    declared = {p["name"] for p in get_op.get("parameters", [])}
    expected = {"status", "aizk_uuid", "created_after", "created_before", "limit", "offset"}
    assert expected.issubset(declared), f"Missing query params: {expected - declared}"


def test_bulk_actions_request_schema_shape():
    """POST /v1/jobs/actions request body must declare action + job_ids fields."""
    app_spec = create_app().openapi()

    if not app_spec.get("components"):
        pytest.xfail("API schemas not registered in FastAPI app yet.")

    schema = app_spec["components"]["schemas"]["BulkJobActionRequest"]
    assert set(schema["properties"].keys()) >= {"action", "job_ids"}
    assert set(schema.get("required", [])) >= {"action", "job_ids"}


def test_openapi_source_ref_unions_are_distinct():
    """JobSubmission.source_ref (narrow) and JobResponse.source_ref (wide) are distinct unions.

    - JobSubmission.source_ref discriminator maps only 'karakeep_bookmark'.
    - JobResponse.source_ref discriminator maps all 6 kinds.
    - The two schema objects are not identical.
    """
    app_spec = create_app().openapi()

    if not app_spec.get("components"):
        pytest.xfail("API schemas not registered in FastAPI app yet.")

    schemas = app_spec["components"]["schemas"]

    # --- JobSubmission.source_ref (narrow ingress union) ---
    submission_source_ref = schemas["JobSubmission"]["properties"]["source_ref"]
    assert "discriminator" in submission_source_ref, "JobSubmission.source_ref must have a discriminator"
    submission_discriminator = submission_source_ref["discriminator"]
    assert submission_discriminator["propertyName"] == "kind"
    assert set(submission_discriminator["mapping"].keys()) == {"karakeep_bookmark"}, (
        f"IngressSourceRef must admit only 'karakeep_bookmark'; got {set(submission_discriminator['mapping'].keys())}"
    )

    # --- JobResponse.source_ref (wide union with all 6 variants) ---
    response_source_ref = schemas["JobResponse"]["properties"]["source_ref"]
    assert "discriminator" in response_source_ref, "JobResponse.source_ref must have a discriminator"
    response_discriminator = response_source_ref["discriminator"]
    assert response_discriminator["propertyName"] == "kind"
    expected_response_kinds = {"karakeep_bookmark", "arxiv", "github_readme", "url", "singlefile", "inline_html"}
    assert set(response_discriminator["mapping"].keys()) == expected_response_kinds, (
        f"SourceRef must contain all 6 kinds; got {set(response_discriminator['mapping'].keys())}"
    )

    # --- The two schemas must be distinct ---
    assert submission_source_ref != response_source_ref, (
        "IngressSourceRef (narrow) and SourceRef (wide) must be distinct OpenAPI schemas"
    )
