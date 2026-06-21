"""Tests for the contextualization operator HTTP API (list/detail/retry/cancel).

Drives the real FastAPI app over a per-test SQLite database: the app's database
URL is pointed at the test DB via ``AIZK_DATABASE_URL`` so the **real**
``get_db_session`` and ``get_principal`` dependencies run (no overrides), and the
work-unit + pipeline tables are created on that same engine. Asserts the read
views, the retry re-queue and cancel transitions (with their co-committed
events), and the 404 / 409 guards.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, SQLModel, select

from fastapi.testclient import TestClient

from aizk.db.engine import get_engine
from aizk.graph.api.main import create_app
from aizk.graph.datamodel import ContextualizationJob
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus

_AIZK_UUID = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A per-test SQLite engine the app also resolves to via ``AIZK_DATABASE_URL``."""
    url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("AIZK_DATABASE_URL", url)
    eng = get_engine(url)
    SQLModel.metadata.create_all(eng, tables=[ContextualizationJob.__table__, PipelineEvent.__table__])
    return eng


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    """A TestClient over the real app (no dependency overrides; same DB as ``engine``)."""
    with TestClient(create_app(), base_url="http://localhost") as test_client:
        yield test_client


def _seed(engine, *, conversion_output_id: int, status: WorkUnitStatus, attempts: int = 0) -> int:
    """Insert one work-unit in the given status; return its id."""
    with Session(engine) as session:
        job = ContextualizationJob(
            idempotency_key=f"conversion_output:{conversion_output_id}",
            conversion_output_id=conversion_output_id,
            aizk_uuid=_AIZK_UUID,
            status=status,
            attempts=attempts,
        )
        session.add(job)
        session.commit()
        return job.id


def test_list_and_filter_by_status(client: TestClient, engine) -> None:
    """The list view returns all units and honors a status filter."""
    _seed(engine, conversion_output_id=1, status=WorkUnitStatus.QUEUED)
    _seed(engine, conversion_output_id=2, status=WorkUnitStatus.SUCCEEDED)
    _seed(engine, conversion_output_id=3, status=WorkUnitStatus.FAILED)

    all_resp = client.get("/v1/contextualizations")
    assert all_resp.status_code == 200
    assert all_resp.json()["total"] == 3

    failed_resp = client.get("/v1/contextualizations", params={"status": "failed"})
    body = failed_resp.json()
    assert body["total"] == 1
    assert body["jobs"][0]["conversion_output_id"] == 3
    assert body["jobs"][0]["status"] == "failed"


def test_detail_and_404(client: TestClient, engine) -> None:
    """Detail returns one unit; an unknown id is 404."""
    job_id = _seed(engine, conversion_output_id=1, status=WorkUnitStatus.QUEUED)

    ok = client.get(f"/v1/contextualizations/{job_id}")
    assert ok.status_code == 200
    assert ok.json()["id"] == job_id
    assert str(_AIZK_UUID) == ok.json()["aizk_uuid"]

    assert client.get("/v1/contextualizations/9999").status_code == 404


def test_retry_requeues_a_terminal_unit(client: TestClient, engine) -> None:
    """Retry moves a FAILED unit back to QUEUED, clears error/wait, and records the event."""
    job_id = _seed(engine, conversion_output_id=1, status=WorkUnitStatus.FAILED, attempts=2)

    resp = client.post(f"/v1/contextualizations/{job_id}/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        assert job.status is WorkUnitStatus.QUEUED
        assert job.earliest_next_attempt_at is None
        assert job.finished_at is None
        events = session.exec(select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id))).all()
        assert [e.kind for e in events] == ["requeued"]
        assert events[0].to_status == "queued"
        assert events[0].aizk_uuid == _AIZK_UUID


def test_retry_rejects_non_terminal_unit(client: TestClient, engine) -> None:
    """Retrying a unit that is not in a re-queueable status is a 409."""
    job_id = _seed(engine, conversion_output_id=1, status=WorkUnitStatus.SUCCEEDED)
    resp = client.post(f"/v1/contextualizations/{job_id}/retry")
    assert resp.status_code == 409


def test_cancel_marks_unit_cancelled(client: TestClient, engine) -> None:
    """Cancel writes a terminal CANCELLED status and records the event."""
    job_id = _seed(engine, conversion_output_id=1, status=WorkUnitStatus.QUEUED)

    resp = client.post(f"/v1/contextualizations/{job_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        assert job.status is WorkUnitStatus.CANCELLED
        assert job.finished_at is not None
        events = session.exec(select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id))).all()
        assert [e.kind for e in events] == ["cancelled"]


def test_cancel_rejects_terminal_unit(client: TestClient, engine) -> None:
    """Cancelling an already-succeeded unit is a 409."""
    job_id = _seed(engine, conversion_output_id=1, status=WorkUnitStatus.SUCCEEDED)
    resp = client.post(f"/v1/contextualizations/{job_id}/cancel")
    assert resp.status_code == 409


def test_rejects_request_with_untrusted_host(engine) -> None:
    """A request whose Host is not in the allowlist is rejected (400) before routing.

    The graph operator API mirrors the conversion API's trusted-host perimeter; the
    default allowlist is localhost/127.0.0.1, so a foreign Host never reaches a
    handler even though the route otherwise exists.
    """
    with TestClient(create_app(), base_url="http://evil.example.com") as foreign_client:
        resp = foreign_client.get("/v1/contextualizations")
    assert resp.status_code == 400
