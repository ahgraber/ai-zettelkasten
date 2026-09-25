"""Pin the OpenAPI identity of the graph stages' work-unit routes."""

from __future__ import annotations

import pytest

from aizk.graph.api.main import create_app

_INTAKE = ["201", "503", "200", "404", "422"]
_ACTION = ["200", "422"]
_CTX = ["contextualizations"]
_EXT = ["extractions"]

_ROUTES = [
    ("post", "/v1/contextualizations", "submit_job_v1_contextualizations_post", "Submit Job", _CTX, _INTAKE),
    ("get", "/v1/contextualizations", "list_jobs_v1_contextualizations_get", "List Jobs", _CTX, _ACTION),
    ("get", "/v1/contextualizations/{job_id}", "get_job_v1_contextualizations__job_id__get", "Get Job", _CTX, _ACTION),
    (
        "post",
        "/v1/contextualizations/{job_id}/retry",
        "retry_job_v1_contextualizations__job_id__retry_post",
        "Retry Job",
        _CTX,
        _ACTION,
    ),
    (
        "post",
        "/v1/contextualizations/{job_id}/cancel",
        "cancel_job_v1_contextualizations__job_id__cancel_post",
        "Cancel Job",
        _CTX,
        _ACTION,
    ),
    ("post", "/v1/extractions", "submit_job_v1_extractions_post", "Submit Job", _EXT, _INTAKE),
    ("get", "/v1/extractions", "list_jobs_v1_extractions_get", "List Jobs", _EXT, _ACTION),
    ("get", "/v1/extractions/{job_id}", "get_job_v1_extractions__job_id__get", "Get Job", _EXT, _ACTION),
    (
        "post",
        "/v1/extractions/{job_id}/retry",
        "retry_job_v1_extractions__job_id__retry_post",
        "Retry Job",
        _EXT,
        _ACTION,
    ),
    (
        "post",
        "/v1/extractions/{job_id}/cancel",
        "cancel_job_v1_extractions__job_id__cancel_post",
        "Cancel Job",
        _EXT,
        _ACTION,
    ),
]


@pytest.fixture(scope="module")
def openapi_paths() -> dict[str, dict[str, dict[str, object]]]:
    """The operator app's generated OpenAPI paths."""
    return create_app().openapi()["paths"]


@pytest.mark.parametrize(
    ("method", "path", "operation_id", "summary", "tags", "responses"),
    _ROUTES,
    ids=[f"{method.upper()} {path}" for method, path, *_ in _ROUTES],
)
def test_a_stage_route_keeps_its_operation_identity(
    openapi_paths: dict[str, dict[str, dict[str, object]]],
    method: str,
    path: str,
    operation_id: str,
    summary: str,
    tags: list[str],
    responses: list[str],
) -> None:
    """A generated client sees the same operation id, summary, tags, and response order per route."""
    operation = openapi_paths[path][method]

    assert operation["operationId"] == operation_id
    assert operation["summary"] == summary
    assert operation["tags"] == tags
    assert list(operation["responses"]) == responses


@pytest.mark.parametrize(
    ("path", "response_schema"),
    [("/v1/contextualizations", "ContextualizationJobResponse"), ("/v1/extractions", "ExtractionJobResponse")],
)
def test_a_stage_exposes_retry_class_as_a_nullable_enum_and_a_list_filter(path: str, response_schema: str) -> None:
    """The job response carries an optional nullable ``retry_class`` enum and the list route takes it as a filter."""
    document = create_app().openapi()
    nullable_retry_class = {"anyOf": [{"$ref": "#/components/schemas/RetryClass"}, {"type": "null"}]}

    schema = document["components"]["schemas"][response_schema]
    assert schema["properties"]["retry_class"] == nullable_retry_class
    assert "retry_class" not in schema["required"]
    assert document["components"]["schemas"]["RetryClass"]["enum"] == ["retryable", "permanent"]
    parameters = {parameter["name"]: parameter for parameter in document["paths"][path]["get"]["parameters"]}
    assert list(parameters) == ["status", "retry_class", "limit", "offset"]
    assert parameters["retry_class"]["schema"]["anyOf"] == nullable_retry_class["anyOf"]
