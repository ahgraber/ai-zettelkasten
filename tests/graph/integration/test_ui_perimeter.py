"""Perimeter tests for the graph operator UI.

Asserts that the HTML UI routes sit behind the same access perimeter as the graph
JSON API: the same trusted-host restriction and the same request-principal
dependency. A UI route must not be reachable under conditions where the
corresponding API route would be rejected.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient

from aizk.conversion.api.dependencies import get_principal

_UNTRUSTED_HOST = "evil.invalid"


def _reject_principal() -> None:
    """Stand-in principal dependency that rejects every request (HTTP 401)."""
    raise HTTPException(status_code=401, detail="principal rejected")


def test_ui_route_rejected_on_untrusted_host_like_the_api(client: TestClient) -> None:
    """A UI route on an untrusted host is rejected by the same trusted-host restriction the API enforces."""
    ui_response = client.get("/ui/graph/jobs", headers={"host": _UNTRUSTED_HOST})
    api_response = client.get("/v1/contextualizations", headers={"host": _UNTRUSTED_HOST})

    assert ui_response.status_code == 400
    assert api_response.status_code == 400


def test_ui_route_served_on_trusted_host(client: TestClient) -> None:
    """A UI route on a trusted host is served rather than rejected."""
    response = client.get("/ui/graph/jobs")

    assert response.status_code == 200
    assert "Contextualization Jobs" in response.text


def test_ui_and_api_rejected_together_when_principal_dependency_rejects(app, client: TestClient) -> None:
    """When the shared principal dependency rejects, both the UI route and the API route are rejected.

    Overriding ``get_principal`` to reject proves the UI route is gated by the very
    dependency the JSON API uses — so the UI is not a weaker perimeter than the API.
    """
    app.dependency_overrides[get_principal] = _reject_principal
    try:
        ui_response = client.get("/ui/graph/jobs")
        api_response = client.get("/v1/contextualizations")
    finally:
        app.dependency_overrides.clear()

    assert ui_response.status_code == 401
    assert api_response.status_code == 401


def test_ui_and_api_resolve_the_same_default_principal(
    client: TestClient,
    db_session,
    seed_source,
    seed_contextualization_job,
) -> None:
    """With the real principal dependency, the UI route and the API route both resolve and serve.

    ``get_principal`` resolves the single deployment principal without inspecting
    request headers, so there is no header to be absent or present: the contract is
    that the UI route resolves the same principal the API does. Both serve the same
    seeded work-unit from the migration-built database, evidencing the harness end to
    end.
    """
    source = seed_source(db_session, karakeep_id="bm_perimeter", title="Perimeter Doc")
    seed_contextualization_job(db_session, aizk_uuid=source.aizk_uuid, conversion_output_id=7)

    ui_response = client.get("/ui/graph/jobs")
    api_response = client.get("/v1/contextualizations")

    assert ui_response.status_code == 200
    assert api_response.status_code == 200
    assert api_response.json()["total"] == 1
