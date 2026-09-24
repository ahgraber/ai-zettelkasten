"""Pin the exact status and JSON body of the literal route error responses.

These bodies are written by hand in the route modules rather than derived from a
response model, so they are invisible to the generated OpenAPI documents and an
operator client that switches on ``detail.error`` would break silently. Each test
drives the real app and asserts the whole body, so a reworded message or a moved
error code shows up as a diff here.

The app harness (hermetic environment, migrated database, session) comes from the
conversion suite through this directory's conftest; both apps read the same
database URL from it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from tests.conversion._helpers import make_job, make_source

from fastapi.testclient import TestClient

from aizk.console.explorer import get_blob_reader
from aizk.conversion.api.main import create_app as create_conversion_app
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.graph.api.main import create_app as create_graph_app
from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine
    from sqlmodel import Session

#: A work-unit / output row id no test seeds, so every lookup on it misses.
_ABSENT_ROW_ID = 987654

#: A source identity no test seeds.
_ABSENT_SOURCE_ID = UUID("44444444-4444-4444-8444-444444444444")

#: The source identity the seeded graph work-units carry.
_GRAPH_UNIT_SOURCE_ID = UUID("77777777-7777-4777-8777-777777777777")

#: The graph intake route prefixes, keyed by the stage each serves.
_GRAPH_ROUTE_PREFIXES = {"contextualization": "/v1/contextualizations", "extraction": "/v1/extractions"}


class _UnusedBlobReader:
    """A blob reader for paths that answer before any Markdown is read."""

    def get_object_bytes(self, s3_key: str) -> bytes:
        """Fail loudly: reaching storage means the test drove the wrong path."""
        raise AssertionError(f"unexpected blob read for {s3_key}")


@pytest.fixture
def conversion_client(db_engine: "Engine") -> "Iterator[TestClient]":
    """A client over the real conversion API on the migrated test database."""
    with TestClient(create_conversion_app()) as client:
        yield client


@pytest.fixture
def graph_client(db_engine: "Engine") -> "Iterator[TestClient]":
    """A client over the real graph operator app on the migrated test database."""
    app = create_graph_app()
    app.dependency_overrides[get_blob_reader] = _UnusedBlobReader
    with TestClient(app) as client:
        yield client


@pytest.fixture
def succeeded_job(db_session: "Session") -> ConversionJob:
    """A job in the one status that is neither retryable nor cancellable."""
    source = make_source(db_session, "bm_error_bodies")
    return make_job(
        db_session,
        source_id=source.source_id,
        status=ConversionJobStatus.SUCCEEDED,
        idempotency_key="c" * 64,
    )


@pytest.fixture
def succeeded_graph_unit_ids(db_session: "Session") -> dict[str, int]:
    """One work-unit per graph stage in the one status that is neither retryable nor cancellable, keyed by stage."""
    contextualization = ContextualizationJob(
        idempotency_key="conversion_output:987",
        conversion_output_id=987,
        source_id=_GRAPH_UNIT_SOURCE_ID,
        status=WorkUnitStatus.SUCCEEDED,
    )
    extraction = ExtractionJob(
        idempotency_key=f"source:{_GRAPH_UNIT_SOURCE_ID}",
        source_id=_GRAPH_UNIT_SOURCE_ID,
        status=WorkUnitStatus.SUCCEEDED,
    )
    db_session.add(contextualization)
    db_session.add(extraction)
    db_session.commit()
    return {"contextualization": contextualization.id, "extraction": extraction.id}


def test_explorer_detail_missing_chunk_body(graph_client: TestClient) -> None:
    """The explorer detail panel names the chunk and the source it is not in."""
    source_id = UUID("55555555-5555-4555-8555-555555555555")
    chunk_id = UUID("66666666-6666-4666-8666-666666666666")

    response = graph_client.get(
        "/ui/explore/chunks/detail",
        params={"source_id": str(source_id), "chunk_id": str(chunk_id)},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": f"chunk {chunk_id} not in the active run of {source_id}"}


def test_explorer_search_unknown_kind_body(graph_client: TestClient) -> None:
    """An unrecognized search type filter is refused with a coded body."""
    response = graph_client.post("/ui/explore/chunks/search", data={"query": "anything", "kind": "not-a-kind"})

    assert response.status_code == 400
    assert response.json() == {"detail": {"error": "invalid_kind", "message": "Invalid search type filter"}}


def test_contextualization_work_unit_not_found_body(graph_client: TestClient) -> None:
    """Reading an absent contextualization work-unit names the stage and the id."""
    response = graph_client.get(f"/v1/contextualizations/{_ABSENT_ROW_ID}")

    assert response.status_code == 404
    assert response.json() == {"detail": f"contextualization work-unit {_ABSENT_ROW_ID} not found"}


def test_extraction_work_unit_not_found_body(graph_client: TestClient) -> None:
    """Reading an absent extraction work-unit names the stage and the id."""
    response = graph_client.get(f"/v1/extractions/{_ABSENT_ROW_ID}")

    assert response.status_code == 404
    assert response.json() == {"detail": f"extraction work-unit {_ABSENT_ROW_ID} not found"}


def test_extraction_submission_unknown_source_body(graph_client: TestClient) -> None:
    """Submitting extraction for an unknown source names the source identity."""
    response = graph_client.post("/v1/extractions", json={"source_id": str(_ABSENT_SOURCE_ID)})

    assert response.status_code == 404
    assert response.json() == {"detail": f"source {_ABSENT_SOURCE_ID} not found"}


def test_contextualization_submission_unknown_output_body(graph_client: TestClient) -> None:
    """Submitting contextualization for an unknown output names the output id."""
    response = graph_client.post("/v1/contextualizations", json={"conversion_output_id": _ABSENT_ROW_ID})

    assert response.status_code == 404
    assert response.json() == {"detail": f"conversion output {_ABSENT_ROW_ID} not found"}


@pytest.mark.parametrize("stage", sorted(_GRAPH_ROUTE_PREFIXES))
def test_graph_retry_ineligible_body(
    graph_client: TestClient, succeeded_graph_unit_ids: dict[str, int], stage: str
) -> None:
    """Retrying a succeeded graph work-unit answers 409 with a body naming the status."""
    response = graph_client.post(f"{_GRAPH_ROUTE_PREFIXES[stage]}/{succeeded_graph_unit_ids[stage]}/retry")

    assert response.status_code == 409
    assert response.json() == {"detail": "cannot retry a work-unit in status 'succeeded'"}


@pytest.mark.parametrize("stage", sorted(_GRAPH_ROUTE_PREFIXES))
def test_graph_cancel_ineligible_body(
    graph_client: TestClient, succeeded_graph_unit_ids: dict[str, int], stage: str
) -> None:
    """Cancelling a succeeded graph work-unit answers 409 with a body naming the status."""
    response = graph_client.post(f"{_GRAPH_ROUTE_PREFIXES[stage]}/{succeeded_graph_unit_ids[stage]}/cancel")

    assert response.status_code == 409
    assert response.json() == {"detail": "cannot cancel a work-unit in status 'succeeded'"}


def test_single_retry_ineligible_body(conversion_client: TestClient, succeeded_job: ConversionJob) -> None:
    """Retrying an ineligible job answers 400 with the coded reason."""
    response = conversion_client.post(f"/v1/jobs/{succeeded_job.id}/retry")

    assert response.status_code == 400
    assert response.json() == {"detail": {"error": "job_not_retryable", "message": "Job cannot be retried"}}


def test_single_cancel_ineligible_body(conversion_client: TestClient, succeeded_job: ConversionJob) -> None:
    """Cancelling an ineligible job answers 400 with the coded reason."""
    response = conversion_client.post(f"/v1/jobs/{succeeded_job.id}/cancel")

    assert response.status_code == 400
    assert response.json() == {"detail": {"error": "job_not_cancellable", "message": "Job cannot be cancelled"}}


@pytest.mark.parametrize(
    ("action", "ineligible_code"), [("retry", "job_not_retryable"), ("cancel", "job_not_cancellable")]
)
def test_bulk_action_per_item_error_codes(
    conversion_client: TestClient,
    succeeded_job: ConversionJob,
    action: str,
    ineligible_code: str,
) -> None:
    """A bulk action reports a bare code per item: missing rows and ineligible statuses."""
    response = conversion_client.post(
        "/v1/jobs/actions",
        json={"action": action, "job_ids": [succeeded_job.id, _ABSENT_ROW_ID]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {"success": 0, "errors": 2}
    assert {item["job_id"]: item["error"] for item in payload["results"]} == {
        succeeded_job.id: ineligible_code,
        _ABSENT_ROW_ID: "job_not_found",
    }
    assert {item["status"] for item in payload["results"]} == {"error"}
