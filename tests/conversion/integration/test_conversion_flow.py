"""Integration test for end-to-end conversion flow."""

import pytest

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app


def test_conversion_flow_end_to_end():
    app = create_app()
    if not any(getattr(route, "path", None) == "/v1/jobs" for route in app.router.routes):
        pytest.xfail("Jobs routes not registered in FastAPI app yet.")

    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            json={
                "karakeep_id": "bm_test_001",
                "url": "https://arxiv.org/abs/1706.03762",
                "title": "Attention Is All You Need",
            },
        )
        assert response.status_code == 201
        job = response.json()
        job_id = job["id"]

        from aizk.conversion.workers.worker import process_job

        process_job(job_id)

        job_response = client.get(f"/v1/jobs/{job_id}")
        assert job_response.status_code == 200
        assert job_response.json()["status"] == "SUCCEEDED"
