"""The conversion service serves no HTML — it is JSON-only.

Operator HTML moved to the console app; the conversion service now exposes only its
JSON API. These tests pin that contract: the former ``/ui/jobs*`` HTML paths are
gone (404), no ``/ui`` path appears in the app's OpenAPI, and the app root redirects
to the API docs rather than a jobs page.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app


def test_openapi_has_no_ui_paths() -> None:
    """The conversion app's OpenAPI contains no ``/ui`` path."""
    paths = create_app().openapi()["paths"]

    assert not any(path.startswith("/ui") for path in paths)
    # The JSON API it exposes instead remains present.
    assert any(path.startswith("/v1/") for path in paths)


def test_former_ui_paths_return_404() -> None:
    """The retired HTML jobs page and its bulk-action endpoint are not-found."""
    with TestClient(create_app()) as client:
        jobs_page = client.get("/ui/jobs")
        actions = client.post("/ui/jobs/actions", data={"action": "retry"})

    assert jobs_page.status_code == 404
    assert actions.status_code == 404


def test_root_redirects_to_docs() -> None:
    """The app root redirects to the API docs (the service is JSON-only)."""
    with TestClient(create_app()) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
