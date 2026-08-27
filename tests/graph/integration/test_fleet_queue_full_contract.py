"""Both services refuse at capacity with one rejection shape.

The conversion service's job submission and the graph service's stage intake both
answer a capacity refusal with HTTP 503 carrying ``Retry-After``. Each service's
own tests assert its literal body and header; this drives both at capacity in one
run and compares the two responses to each other, so a divergence is caught here
rather than passing because each side agrees only with itself.

Both apps resolve the same migration-built database from the environment, so the
comparison runs against real routes rather than a shared helper called twice.
"""

from __future__ import annotations

from httpx import Response
import pytest
from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app as create_conversion_app
from aizk.conversion.datamodel.job import ConversionJobStatus
from aizk.graph.api.main import create_app as create_graph_app

#: Set on both services so an equal ``Retry-After`` means agreement, not coincidence.
_RETRY_AFTER = 45


def _conversion_refusal(
    db_session: Session,
    seed_source,
    seed_conversion_job,
    monkeypatch: pytest.MonkeyPatch,
) -> Response:
    """Fill the conversion queue to its declared depth, then submit one more job."""
    monkeypatch.setenv("AIZK_QUEUE_MAX_DEPTH", "1")
    monkeypatch.setenv("AIZK_QUEUE_RETRY_AFTER_SECONDS", str(_RETRY_AFTER))
    source = seed_source(db_session, karakeep_id="fleet_conversion_fill", title="Fill")
    seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="fleet-conversion-fill",
        status=ConversionJobStatus.QUEUED,
    )

    with TestClient(create_conversion_app()) as client:
        return client.post(
            "/v1/jobs",
            json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": "fleet_conversion_new"}},
        )


def _graph_refusal(
    db_session: Session,
    seed_source,
    seed_conversion_job,
    seed_conversion_output,
    monkeypatch: pytest.MonkeyPatch,
) -> Response:
    """Fill contextualization to its declared capacity, then submit one more output."""
    monkeypatch.setenv("AIZK_GRAPH__CONTEXTUALIZATION_QUEUE_MAX_DEPTH", "1")
    monkeypatch.setenv("AIZK_GRAPH__QUEUE_RETRY_AFTER_SECONDS", str(_RETRY_AFTER))
    source = seed_source(db_session, karakeep_id="fleet_graph", title="Doc")
    outputs = []
    for index in range(2):
        # Finished conversion jobs: the outputs need a parent row, and a queued one
        # would land in the conversion service's backlog rather than the graph's.
        job = seed_conversion_job(
            db_session,
            source_id=source.source_id,
            idempotency_key=f"fleet-graph-{index}",
            status=ConversionJobStatus.SUCCEEDED,
        )
        outputs.append(seed_conversion_output(db_session, job_id=job.id, source_id=source.source_id))

    with TestClient(create_graph_app()) as client:
        accepted = client.post("/v1/contextualizations", json={"conversion_output_id": outputs[0].id})
        assert accepted.status_code == 201, "the first submission must fill the stage, not be refused"
        return client.post("/v1/contextualizations", json={"conversion_output_id": outputs[1].id})


def test_both_services_refuse_at_capacity_with_the_same_rejection(
    db_session: Session,
    seed_source,
    seed_conversion_job,
    seed_conversion_output,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One convention covers the fleet: same status, same body, same backoff header.

    A client that learns to back off from one service backs off from the other
    without a second code path.
    """
    conversion = _conversion_refusal(db_session, seed_source, seed_conversion_job, monkeypatch)
    graph = _graph_refusal(db_session, seed_source, seed_conversion_job, seed_conversion_output, monkeypatch)

    assert (conversion.status_code, graph.status_code) == (503, 503)
    assert conversion.json() == graph.json()
    assert conversion.headers["Retry-After"] == graph.headers["Retry-After"] == str(_RETRY_AFTER)
