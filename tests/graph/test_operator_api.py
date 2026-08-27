"""Tests for the contextualization operator HTTP API (submit/list/detail/retry/cancel).

Drives the real FastAPI app over a per-test SQLite database: the app's database
URL is pointed at the test DB via ``AIZK_DATABASE_URL`` so the **real**
``get_db_session`` and ``get_principal`` dependencies run (no overrides), and the
work-unit + pipeline tables are created on that same engine. Asserts the intake
submission's create / reuse / not-found / at-capacity matrix, the read views, the
retry re-queue and cancel transitions (with their co-committed events), and the
404 / 409 guards.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import datetime as dt
from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, SQLModel, select

from fastapi.testclient import TestClient

from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.db.engine import get_engine
from aizk.graph.api.main import create_app
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.enqueue import enqueue_output
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus

_AIZK_UUID = UUID("11111111-1111-1111-1111-111111111111")
_EPOCH = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A per-test SQLite engine the app also resolves to via ``AIZK_DATABASE_URL``."""
    url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("AIZK_DATABASE_URL", url)
    eng = get_engine(url)
    SQLModel.metadata.create_all(
        eng,
        tables=[
            ContextualizationJob.__table__,
            # ``conversion_outputs`` carries foreign keys to both, and the app's engine
            # enforces them, so an intake submission's upstream row needs its parents.
            ConversionJob.__table__,
            Source.__table__,
            ConversionOutput.__table__,
            PipelineEvent.__table__,
        ],
    )
    return eng


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    """A TestClient over the real app (no dependency overrides; same DB as ``engine``)."""
    with TestClient(create_app(), base_url="http://localhost") as test_client:
        yield test_client


@pytest.fixture
def client_factory(engine, monkeypatch: pytest.MonkeyPatch):
    """Build a TestClient after setting per-test admission environment variables.

    The app reads its admission settings during lifespan, so a declared capacity
    must be in the environment before the client is constructed.
    """

    @contextlib.contextmanager
    def _build(**settings: object) -> "Iterator[TestClient]":
        for name, value in settings.items():
            monkeypatch.setenv(name, str(value))
        with TestClient(create_app(), base_url="http://localhost") as test_client:
            yield test_client

    return _build


def _add_output(engine, *, output_id: int, source_id: UUID = _AIZK_UUID) -> None:
    """Insert the upstream conversion output an intake submission references, with its parent rows."""
    with Session(engine) as session:
        if session.exec(select(Source).where(Source.source_id == source_id)).first() is None:
            session.add(
                Source(
                    source_id=source_id,
                    source_ref=f'{{"kind":"url","url":"https://example.test/{source_id}"}}',
                    source_ref_hash=str(source_id),
                    owner_id="owner",
                    title="Doc",
                )
            )
        session.add(
            ConversionJob(
                id=output_id,
                source_id=source_id,
                owner_id="owner",
                title="Doc",
                payload_version=1,
                idempotency_key=f"job-{output_id}",
                source_ref=f'{{"kind":"url","url":"https://example.test/{source_id}"}}',
            )
        )
        session.flush()
        session.add(
            ConversionOutput(
                id=output_id,
                job_id=output_id,
                source_id=source_id,
                owner_id="owner",
                title="Doc",
                payload_version=1,
                s3_prefix=f"prefix-{output_id}",
                markdown_key=f"prefix-{output_id}/output.md",
                manifest_key=f"prefix-{output_id}/manifest.json",
                markdown_hash_xx64="0011223344556677",
                docling_version="1.0",
                pipeline_name="docling",
                created_at=_EPOCH,
            )
        )
        session.commit()


def _seed(engine, *, conversion_output_id: int, status: WorkUnitStatus, attempts: int = 0) -> int:
    """Insert one work-unit in the given status; return its id."""
    with Session(engine) as session:
        job = ContextualizationJob(
            idempotency_key=f"conversion_output:{conversion_output_id}",
            conversion_output_id=conversion_output_id,
            source_id=_AIZK_UUID,
            status=status,
            attempts=attempts,
        )
        session.add(job)
        session.commit()
        return job.id


def test_submit_creates_a_queued_work_unit(client: TestClient, engine) -> None:
    """A submission for work with no unit yet creates one, queued for processing."""
    _add_output(engine, output_id=7)

    response = client.post("/v1/contextualizations", json={"conversion_output_id": 7})

    assert response.status_code == 201
    body = response.json()
    assert body["conversion_output_id"] == 7
    assert body["source_id"] == str(_AIZK_UUID)
    assert body["status"] == "queued"
    with Session(engine) as session:
        assert len(session.exec(select(ContextualizationJob)).all()) == 1


def test_resubmitting_returns_the_existing_unit(client: TestClient, engine) -> None:
    """The same work submitted twice returns the original unit rather than duplicating it."""
    _add_output(engine, output_id=7)
    first = client.post("/v1/contextualizations", json={"conversion_output_id": 7})

    second = client.post("/v1/contextualizations", json={"conversion_output_id": 7})

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    with Session(engine) as session:
        assert len(session.exec(select(ContextualizationJob)).all()) == 1


def test_submitting_an_unknown_output_is_rejected_cleanly(client: TestClient, engine) -> None:
    """A submission naming no existing conversion output is a 404 that changes nothing."""
    response = client.post("/v1/contextualizations", json={"conversion_output_id": 404})

    assert response.status_code == 404
    with Session(engine) as session:
        assert session.exec(select(ContextualizationJob)).all() == []


@pytest.mark.parametrize("output_id", [0, -1, 2**63], ids=["zero", "negative", "beyond-rowid"])
def test_an_id_outside_the_key_range_is_a_rejection_not_a_crash(client: TestClient, engine, output_id: int) -> None:
    """An id that cannot name a row is refused at the boundary rather than reaching SQLite."""
    response = client.post("/v1/contextualizations", json={"conversion_output_id": output_id})

    assert response.status_code == 422
    with Session(engine) as session:
        assert session.exec(select(ContextualizationJob)).all() == []


def test_submit_refuses_new_work_at_capacity(client_factory, engine) -> None:
    """At the stage's declared capacity a new submission is refused with the fleet's rejection shape."""
    _add_output(engine, output_id=1)
    _add_output(engine, output_id=2, source_id=UUID("22222222-2222-2222-2222-222222222222"))
    with client_factory(
        AIZK_GRAPH__CONTEXTUALIZATION_QUEUE_MAX_DEPTH=1, AIZK_GRAPH__QUEUE_RETRY_AFTER_SECONDS=45
    ) as client:
        assert client.post("/v1/contextualizations", json={"conversion_output_id": 1}).status_code == 201

        response = client.post("/v1/contextualizations", json={"conversion_output_id": 2})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "45"
    assert response.json() == {"detail": "Queue is at capacity", "retry_after": 45}
    with Session(engine) as session:
        assert len(session.exec(select(ContextualizationJob)).all()) == 1


def test_a_duplicate_submission_at_capacity_still_succeeds(client_factory, engine) -> None:
    """Resubmitting work that already has a unit adds nothing to the backlog, so it is not refused."""
    _add_output(engine, output_id=1)
    with client_factory(AIZK_GRAPH__CONTEXTUALIZATION_QUEUE_MAX_DEPTH=1) as client:
        first = client.post("/v1/contextualizations", json={"conversion_output_id": 1})

        again = client.post("/v1/contextualizations", json={"conversion_output_id": 1})

    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]


def test_an_intake_unit_equals_a_domain_enqueued_unit(client: TestClient, engine) -> None:
    """A unit intake created is indistinguishable from one the stage's domain enqueue created."""
    _add_output(engine, output_id=1)
    _add_output(engine, output_id=2, source_id=UUID("22222222-2222-2222-2222-222222222222"))

    client.post("/v1/contextualizations", json={"conversion_output_id": 1})
    with Session(engine) as session:
        enqueue_output(session, 2, queue_max_depth=0)
        session.commit()

    with Session(engine) as session:
        by_output = {job.conversion_output_id: job for job in session.exec(select(ContextualizationJob)).all()}

        def _fields(job: ContextualizationJob) -> tuple:
            return (job.status, job.attempts, job.error_code, job.error_message, job.queued_at is not None)

        assert by_output[1].idempotency_key == "conversion_output:1"
        assert by_output[2].idempotency_key == "conversion_output:2"
        assert _fields(by_output[1]) == _fields(by_output[2])


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
    assert str(_AIZK_UUID) == ok.json()["source_id"]

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
        assert events[0].source_id == _AIZK_UUID


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
