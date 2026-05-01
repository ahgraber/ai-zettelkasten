"""Integration tests for owner_id materialization at the API trust boundary.

Pins the materialization contract from the deployment-trust-model change:

- Source + Job rows carry `owner_id = principal.subject` from `get_principal`.
- Source reuse preserves the first writer's `owner_id` (no `DO UPDATE` clause).
- The OpenAPI schema for `POST /v1/jobs` does NOT expose `owner_id`,
  confirming the column is internal-only.
"""

from __future__ import annotations

from sqlmodel import select

from fastapi.testclient import TestClient

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.api.main import create_app
from aizk.conversion.auth import Principal
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.source import Source


def _submit(client: TestClient, bookmark_id: str) -> dict:
    return client.post(
        "/v1/jobs",
        json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}},
    )


def test_submit_persists_principal_subject_on_source_and_job(db_session, monkeypatch) -> None:
    """A submission under `AIZK_DEFAULT_PRINCIPAL=alice` stamps `alice` on Source + Job."""
    monkeypatch.setenv("AIZK_DEFAULT_PRINCIPAL", "alice")
    app = create_app()
    with TestClient(app) as client:
        resp = _submit(client, "bm_owner_alice")

    assert resp.status_code == 201
    source = db_session.exec(select(Source).where(Source.karakeep_id == "bm_owner_alice")).one()
    job = db_session.exec(select(ConversionJob).where(ConversionJob.aizk_uuid == source.aizk_uuid)).one()
    assert source.owner_id == "alice"
    assert job.owner_id == "alice"


def test_source_reuse_preserves_first_writers_owner_id(db_session) -> None:
    """Two submissions for the same source_ref under different principals: Source keeps first owner; Jobs do not collapse.

    Override `get_principal` per-call via FastAPI's dependency_overrides so the
    same TestClient sees a different Principal across the two POSTs. Asserts:

    - the Source row's ``owner_id`` is the FIRST writer's subject (INSERT OR IGNORE
      preserves the first-writer's principal);
    - duplicate-submission detection is owner-scoped, so bob's submission does
      NOT replay alice's job and instead creates a new Job row owned by bob,
      while reusing the same Source row.
    """
    app = create_app()
    bookmark_id = "bm_owner_reuse_race"

    current_subject = {"value": "alice"}

    def _override_principal() -> Principal:
        return Principal(subject=current_subject["value"], provenance="trust_network")

    app.dependency_overrides[get_principal] = _override_principal

    with TestClient(app) as client:
        first = _submit(client, bookmark_id)
        current_subject["value"] = "bob"
        second = _submit(client, bookmark_id)

    assert first.status_code == 201
    assert second.status_code == 201, "cross-owner submission must create a new job, not replay alice's"
    assert first.json()["aizk_uuid"] == second.json()["aizk_uuid"], "Source row is shared across principals"
    assert first.json()["id"] != second.json()["id"], "Job row is per-owner"

    sources = db_session.exec(select(Source).where(Source.karakeep_id == bookmark_id)).all()
    assert len(sources) == 1
    assert sources[0].owner_id == "alice", "Source owner_id must be the first writer (alice), not last (bob)"

    jobs = db_session.exec(select(ConversionJob).where(ConversionJob.aizk_uuid == sources[0].aizk_uuid)).all()
    owners = sorted(j.owner_id for j in jobs)
    assert owners == ["alice", "bob"]


def test_openapi_schema_excludes_owner_id_from_jobs_endpoint() -> None:
    """The OpenAPI schema for /v1/jobs must not surface owner_id in any request or response field.

    `owner_id` is an internal-only column — it must never appear in client-facing
    request bodies, response bodies, or schema definitions reachable from the
    /v1/jobs path. Adding `owner_id` to a Pydantic schema that backs /v1/jobs
    is a deliberate change that requires removing or rewriting this test.
    """
    app = create_app()
    schema = app.openapi()

    # Walk every schema component reachable from /v1/jobs and assert no
    # `owner_id` field appears in any properties dict.
    components = schema.get("components", {}).get("schemas", {})
    offenders: list[str] = []
    for name, definition in components.items():
        properties = definition.get("properties", {}) if isinstance(definition, dict) else {}
        if "owner_id" in properties:
            offenders.append(name)
    assert not offenders, (
        f"owner_id leaked into OpenAPI schema components: {offenders}. "
        "owner_id is internal-only — see deployment-trust-model/specs/conversion-api/spec.md."
    )

    # Belt-and-braces: the raw JSON of the /v1/jobs paths must not contain the literal string.
    import json

    jobs_paths = {p: spec for p, spec in schema.get("paths", {}).items() if p.startswith("/v1/jobs")}
    assert "owner_id" not in json.dumps(jobs_paths), "owner_id appears in /v1/jobs path schema"
