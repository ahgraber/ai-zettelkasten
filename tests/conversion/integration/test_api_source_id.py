"""The conversion API names the durable source identity ``source_id`` on its public surface.

Evidence for the conversion-api rename: the public OpenAPI contract, the
bookmark-outputs route, and the job-list filter use ``source_id`` (never
``aizk_uuid``), and the materialized ``source_id`` is a distinct identity surrogate
from the ``source_ref_hash`` dedup key. The end-to-end route/dedup behaviors
themselves are covered by ``test_bookmark_outputs`` / ``test_jobs_list`` /
``test_source_dedup``; this module pins the identity-naming contract.
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlmodel import select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.source import Source


def _submit(client: TestClient, bookmark_id: str):
    """Submit a KarakeepBookmarkRef conversion job and return the response."""
    return client.post("/v1/jobs", json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}})


def test_openapi_surface_uses_source_id() -> None:
    """The public OpenAPI contract names the source identity ``source_id``, not ``aizk_uuid``."""
    spec = create_app().openapi()

    assert "/v1/bookmarks/{source_id}/outputs" in spec["paths"]
    assert "/v1/bookmarks/{aizk_uuid}/outputs" not in spec["paths"]

    job_list_params = {p["name"] for p in spec["paths"]["/v1/jobs"]["get"].get("parameters", [])}
    assert "source_id" in job_list_params, "the job-list filter parameter is source_id"
    assert "aizk_uuid" not in job_list_params

    assert "aizk_uuid" not in json.dumps(spec), "the pre-rename identity name appears nowhere in the contract"


def test_source_identity_is_distinct_surrogate_from_dedup_key(db_session) -> None:
    """Materialization persists a ``source_id`` UUID identity, distinct from the ``source_ref_hash`` dedup key."""
    app = create_app()
    with TestClient(app) as client:
        first = _submit(client, "bm_identity")
        second = _submit(client, "bm_identity")

    assert first.status_code == 201
    assert second.status_code in (200, 201)

    source = db_session.exec(select(Source).where(Source.karakeep_id == "bm_identity")).one()
    response_source_id = first.json()["source_id"]

    # The durable identity is a surrogate UUID, exposed as source_id and reused across submissions.
    assert UUID(response_source_id) == source.source_id
    assert second.json()["source_id"] == response_source_id, "same source_ref reuses the source_id identity"
    # The dedup/sameness key is a separate content fingerprint, never the identity.
    assert source.source_ref_hash and source.source_ref_hash != str(source.source_id)


def test_bookmark_outputs_route_and_job_list_filter_operate_on_source_id(db_session) -> None:
    """The bookmark-outputs route and the job-list filter both key on ``source_id``."""
    app = create_app()
    with TestClient(app) as client:
        submitted = _submit(client, "bm_route_filter")
        source_id = submitted.json()["source_id"]

        filtered = client.get("/v1/jobs", params={"source_id": source_id})
        assert filtered.status_code == 200
        assert [job["id"] for job in filtered.json()["jobs"]] == [submitted.json()["id"]]

        outputs = client.get(f"/v1/bookmarks/{source_id}/outputs")
        assert outputs.status_code == 200, "the route accepts a source_id path parameter"
        assert outputs.json() == [], "a freshly submitted source has no outputs yet"
