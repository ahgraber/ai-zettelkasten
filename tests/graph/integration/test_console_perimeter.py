"""Perimeter tests for the operator console's unified task routes.

Asserts the console's monitor, drill-down, and action routes sit behind the same
access perimeter as the JSON APIs: the same trusted-host restriction and the same
request-principal dependency. A console route must not be reachable under
conditions where the corresponding API route would be rejected. The checks are
parameterized over the registered stages so every stage's monitor is covered.
"""

from __future__ import annotations

import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from aizk.conversion.api.dependencies import get_principal
from aizk.graph.datamodel import ContextualizationJob
from aizk.pipeline.lifecycle import WorkUnitStatus

_UNTRUSTED_HOST = "evil.invalid"
_STAGES = ["conversion", "contextualization", "extraction"]


def _reject_principal() -> None:
    """Stand-in principal dependency that rejects every request (HTTP 401)."""
    raise HTTPException(status_code=401, detail="principal rejected")


@pytest.mark.parametrize("stage", _STAGES)
def test_console_routes_rejected_on_untrusted_host_like_the_api(client: TestClient, stage: str) -> None:
    """The monitor, drill-down, and action routes are all rejected on an untrusted host."""
    headers = {"host": _UNTRUSTED_HOST}
    monitor = client.get("/ui/tasks", params={"stage": stage}, headers=headers)
    drilldown = client.get(f"/ui/tasks/{stage}/1", headers=headers)
    action = client.post(f"/ui/tasks/{stage}/actions", data={"action": "retry"}, headers=headers)
    api = client.get("/v1/contextualizations", headers=headers)

    assert monitor.status_code == 400
    assert drilldown.status_code == 400
    assert action.status_code == 400
    assert api.status_code == 400


@pytest.mark.parametrize("stage", _STAGES)
def test_console_monitor_served_on_trusted_host(client: TestClient, stage: str) -> None:
    """A stage's monitor on a trusted host is served rather than rejected."""
    response = client.get("/ui/tasks", params={"stage": stage})

    assert response.status_code == 200


@pytest.mark.parametrize("stage", _STAGES)
def test_console_and_api_rejected_together_when_principal_dependency_rejects(
    app, client: TestClient, stage: str
) -> None:
    """When the shared principal dependency rejects, the console route is rejected with the API.

    Overriding ``get_principal`` to reject proves the console route is gated by the
    very dependency the JSON API uses — so it is not a weaker perimeter than the API.
    """
    app.dependency_overrides[get_principal] = _reject_principal
    try:
        monitor = client.get("/ui/tasks", params={"stage": stage})
        api = client.get("/v1/contextualizations")
    finally:
        app.dependency_overrides.clear()

    assert monitor.status_code == 401
    assert api.status_code == 401


def test_console_and_api_resolve_the_same_default_principal(
    client: TestClient,
    db_session,
    seed_source,
    seed_contextualization_job,
) -> None:
    """With the real principal dependency, the console route and the API route both serve.

    ``get_principal`` resolves the single deployment principal without inspecting
    request headers, so the contract is that the console route resolves the same
    principal the API does. Both serve the same seeded work-unit from the
    migration-built database, evidencing the harness end to end.
    """
    source = seed_source(db_session, karakeep_id="bm_perimeter", title="Perimeter Doc")
    seed_contextualization_job(db_session, source_id=source.source_id, conversion_output_id=7)

    console = client.get("/ui/tasks", params={"stage": "contextualization"})
    api = client.get("/v1/contextualizations")

    assert console.status_code == 200
    assert "Contextualization" in console.text
    assert api.status_code == 200
    assert api.json()["total"] == 1


def test_actions_rejects_a_browser_cross_origin_post(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """A cross-origin POST to a mutating action route is rejected (403) with no mutation.

    Defense-in-depth over the trusted-host perimeter: a foreign ``Origin`` (a
    cross-site auto-submitting form) cannot drive a bulk action.
    """
    source = seed_source(db_session, karakeep_id="bm_csrf", title="CSRF Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=1, status=WorkUnitStatus.FAILED
    )

    response = client.post(
        "/ui/tasks/contextualization/actions",
        data={"action": "retry", "job_ids": [job.id]},
        headers={"Origin": "http://evil.invalid"},
    )

    assert response.status_code == 403
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, job.id).status is WorkUnitStatus.FAILED


def test_actions_allows_a_same_origin_post(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """A same-origin POST (``Origin`` matches ``Host``) — the console's own form — is served."""
    source = seed_source(db_session, karakeep_id="bm_sameorigin", title="Same Origin Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=2, status=WorkUnitStatus.FAILED
    )

    response = client.post(
        "/ui/tasks/contextualization/actions",
        data={"action": "retry", "job_ids": [job.id]},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, job.id).status is WorkUnitStatus.QUEUED
