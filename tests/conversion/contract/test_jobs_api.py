"""Contract tests for conversion job APIs."""

from pathlib import Path
import yaml

import pytest

from aizk.conversion.api.main import create_app


def _load_openapi_spec() -> dict:
    repo_root = Path(__file__).resolve().parents[3]
    spec_path = repo_root / "specs" / "001-docling-conversion-service" / "contracts" / "openapi.yaml"
    return yaml.safe_load(spec_path.read_text())


def test_jobs_endpoints_match_openapi_contract():
    spec = _load_openapi_spec()
    app = create_app()
    app_spec = app.openapi()

    if not app_spec.get("paths"):
        pytest.xfail("API routes not registered in FastAPI app yet.")

    expected_paths = {
        "/v1/jobs": {"post", "get"},
        "/v1/jobs/{job_id}": {"get"},
    }

    for path, methods in expected_paths.items():
        assert path in spec["paths"]
        assert path in app_spec["paths"]
        for method in methods:
            assert method in spec["paths"][path]
            assert method in app_spec["paths"][path]


def test_jobs_schemas_present_in_openapi():
    spec = _load_openapi_spec()
    app_spec = create_app().openapi()

    if not app_spec.get("components"):
        pytest.xfail("API schemas not registered in FastAPI app yet.")

    for schema_name in ("JobSubmission", "JobResponse", "ConversionJobStatus"):
        assert schema_name in spec["components"]["schemas"]
        assert schema_name in app_spec["components"]["schemas"]
