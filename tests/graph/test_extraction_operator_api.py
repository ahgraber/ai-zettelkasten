"""Tests for the extraction operator HTTP API (list/detail/retry/cancel).

Mirrors ``tests/graph/test_operator_api.py`` exactly, parameterized for
:class:`~aizk.graph.datamodel.ExtractionJob`. Drives the real FastAPI app over a
per-test SQLite database: the app's database URL is pointed at the test DB via
``AIZK_DATABASE_URL`` so the **real** ``get_db_session`` and ``get_principal``
dependencies run (no overrides), and the work-unit + pipeline tables are
created on that same engine.
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
from aizk.graph.datamodel import ExtractionJob
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus

_SOURCE_A = UUID("11111111-1111-1111-1111-111111111111")
_SOURCE_B = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A per-test SQLite engine the app also resolves to via ``AIZK_DATABASE_URL``."""
    url = f"sqlite:///{tmp_path / 'extraction_api.db'}"
    monkeypatch.setenv("AIZK_DATABASE_URL", url)
    eng = get_engine(url)
    SQLModel.metadata.create_all(eng, tables=[ExtractionJob.__table__, PipelineEvent.__table__])
    return eng


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    """A TestClient over the real app (no dependency overrides; same DB as ``engine``)."""
    with TestClient(create_app(), base_url="http://localhost") as test_client:
        yield test_client


def _seed(
    engine, *, source_id: UUID, status: WorkUnitStatus, attempts: int = 0, idempotency_key: str | None = None
) -> int:
    """Insert one work-unit in the given status; return its id."""
    with Session(engine) as session:
        job = ExtractionJob(
            idempotency_key=idempotency_key or f"source:{source_id}",
            source_id=source_id,
            status=status,
            attempts=attempts,
        )
        session.add(job)
        session.commit()
        return job.id


def test_list_and_filter_by_status(client: TestClient, engine) -> None:
    """The list view returns all units and honors a status filter."""
    _seed(engine, source_id=_SOURCE_A, status=WorkUnitStatus.QUEUED, idempotency_key="source:queued")
    _seed(engine, source_id=_SOURCE_B, status=WorkUnitStatus.SUCCEEDED, idempotency_key="source:succeeded")
    failed_id = _seed(engine, source_id=_SOURCE_A, status=WorkUnitStatus.FAILED, idempotency_key="source:failed")

    all_resp = client.get("/v1/extractions")
    assert all_resp.status_code == 200
    assert all_resp.json()["total"] == 3

    failed_resp = client.get("/v1/extractions", params={"status": "failed"})
    body = failed_resp.json()
    assert body["total"] == 1
    assert body["jobs"][0]["id"] == failed_id
    assert body["jobs"][0]["status"] == "failed"


def test_detail_and_404(client: TestClient, engine) -> None:
    """Detail returns one unit; an unknown id is 404."""
    job_id = _seed(engine, source_id=_SOURCE_A, status=WorkUnitStatus.QUEUED)

    ok = client.get(f"/v1/extractions/{job_id}")
    assert ok.status_code == 200
    assert ok.json()["id"] == job_id
    assert str(_SOURCE_A) == ok.json()["source_id"]

    assert client.get("/v1/extractions/9999").status_code == 404


def test_retry_requeues_a_terminal_unit(client: TestClient, engine) -> None:
    """Retry moves a FAILED unit back to QUEUED, clears error/wait, and records the event."""
    job_id = _seed(engine, source_id=_SOURCE_A, status=WorkUnitStatus.FAILED, attempts=2)

    resp = client.post(f"/v1/extractions/{job_id}/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.QUEUED
        assert job.earliest_next_attempt_at is None
        assert job.finished_at is None
        events = session.exec(select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id))).all()
        assert [e.kind for e in events] == ["requeued"]
        assert events[0].to_status == "queued"
        assert events[0].source_id == _SOURCE_A


def test_retry_rejects_non_terminal_unit(client: TestClient, engine) -> None:
    """Retrying a unit that is not in a re-queueable status is a 409."""
    job_id = _seed(engine, source_id=_SOURCE_A, status=WorkUnitStatus.SUCCEEDED)
    resp = client.post(f"/v1/extractions/{job_id}/retry")
    assert resp.status_code == 409


def test_cancel_marks_unit_cancelled(client: TestClient, engine) -> None:
    """Cancel writes a terminal CANCELLED status and records the event."""
    job_id = _seed(engine, source_id=_SOURCE_A, status=WorkUnitStatus.QUEUED)

    resp = client.post(f"/v1/extractions/{job_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.CANCELLED
        assert job.finished_at is not None
        events = session.exec(select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id))).all()
        assert [e.kind for e in events] == ["cancelled"]


def test_cancel_rejects_terminal_unit(client: TestClient, engine) -> None:
    """Cancelling an already-succeeded unit is a 409."""
    job_id = _seed(engine, source_id=_SOURCE_A, status=WorkUnitStatus.SUCCEEDED)
    resp = client.post(f"/v1/extractions/{job_id}/cancel")
    assert resp.status_code == 409


def test_rejects_request_with_untrusted_host(engine) -> None:
    """A request whose Host is not in the allowlist is rejected (400) before routing."""
    with TestClient(create_app(), base_url="http://evil.example.com") as foreign_client:
        resp = foreign_client.get("/v1/extractions")
    assert resp.status_code == 400
