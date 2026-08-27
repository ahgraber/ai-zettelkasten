"""The capacity evaluation shares one write transaction with the creation it guards.

A stage's declared limit is only a bound if the backlog count and the insert that
follows it cannot be interleaved by another writer. Every path that creates graph
work — an admission pass, either backfill command, either intake route — must
therefore already hold the write transaction when it evaluates capacity.

Each test drives one path and probes, at the moment the evaluation runs, whether a
second connection can begin a write. On SQLite a held ``BEGIN IMMEDIATE`` refuses
it, so "locked out" is direct evidence that the evaluation and the creation are
inside one transaction. Each test first confirms the lock is free, so a passing
result cannot come from some unrelated writer holding it.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from pathlib import Path
import sqlite3
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.conversion.datamodel.job import ConversionJobStatus
from aizk.graph import capacity
from aizk.graph.admission import contextualization_adapter, run_admission_pass
from aizk.graph.api.main import create_app
from aizk.graph.backfill import run_contextualization_backfill, run_extraction_backfill
from aizk.graph.config import AdmissionConfig
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.run import PipelineRun, RunStatus

#: Positive, and above anything a test enqueues: the check must run, not refuse.
_LIMIT = 50


def _db_path(graph_db_url: str) -> Path:
    """Return the filesystem path behind the test's ``sqlite:///`` URL."""
    return Path(graph_db_url.removeprefix("sqlite:///"))


def _competing_writer_is_locked_out(db_path: Path) -> bool:
    """Return whether a second connection is refused a write transaction right now."""
    connection = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return True
    else:
        connection.execute("ROLLBACK")
        return False
    finally:
        connection.close()


@contextlib.contextmanager
def _capacity_probe(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> Iterator[dict[str, bool]]:
    """Record, at capacity-evaluation time, whether another writer could interleave.

    Wraps the backlog count every capacity entry point funnels through, so one probe
    covers ``check_capacity``, ``headroom``, and ``within_headroom`` alike.
    """
    observed: dict[str, bool] = {}
    real_backlog = capacity.actionable_backlog

    def _spy(session: Session, model: type) -> int:
        observed["locked_out"] = _competing_writer_is_locked_out(db_path)
        return real_backlog(session, model)

    monkeypatch.setattr(capacity, "actionable_backlog", _spy)
    yield observed


def _seed_converted(db_session: Session, seed_source, seed_conversion_job, seed_conversion_output, tag: str) -> int:
    """Give one source a finished conversion job and its output; return the output id."""
    source = seed_source(db_session, karakeep_id=f"cap_{tag}", title="Doc")
    job = seed_conversion_job(
        db_session,
        source_id=source.source_id,
        idempotency_key=f"cap-{tag}",
        status=ConversionJobStatus.SUCCEEDED,
    )
    return seed_conversion_output(db_session, job_id=job.id, source_id=source.source_id).id


def _seed_chunked(db_session: Session, seed_source, tag: str) -> UUID:
    """Give one source an active chunking run, so it is pending extraction."""
    source = seed_source(db_session, karakeep_id=f"cap_{tag}", title="Doc")
    db_session.add(
        PipelineRun(
            stage=CHUNKING_STAGE,
            scope_id=str(source.source_id),
            status=RunStatus.ACTIVE,
            derivation_key=f"dk-{tag}",
        )
    )
    db_session.commit()
    return source.source_id


def _assert_evaluated_under_the_write_lock(observed: dict[str, bool]) -> None:
    """Assert the probe fired, and that it found the path holding the write lock."""
    assert observed.get("locked_out") is not None, "the capacity evaluation never ran"
    assert observed["locked_out"], "another writer could interleave between the count and the insert"


def test_the_admission_pass_evaluates_capacity_under_its_write_lock(
    migrated_engine: Engine,
    db_session: Session,
    graph_db_url: str,
    seed_source,
    seed_conversion_job,
    seed_conversion_output,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admission pass counts the backlog inside the transaction its enqueue commits."""
    _seed_converted(db_session, seed_source, seed_conversion_job, seed_conversion_output, "admission")
    db_path = _db_path(graph_db_url)
    assert not _competing_writer_is_locked_out(db_path), "precondition: the write lock starts free"
    adapter = contextualization_adapter(
        AdmissionConfig(
            _env_file=None,
            admission_contextualization_enabled=True,
            contextualization_queue_max_depth=_LIMIT,
        )
    )

    with _capacity_probe(monkeypatch, db_path) as observed:
        run_admission_pass(migrated_engine, adapter)

    _assert_evaluated_under_the_write_lock(observed)


def test_the_contextualization_backfill_evaluates_capacity_under_its_write_lock(
    migrated_engine: Engine,
    db_session: Session,
    graph_db_url: str,
    seed_source,
    seed_conversion_job,
    seed_conversion_output,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corpus-wide contextualization backfill counts inside its own transaction."""
    _seed_converted(db_session, seed_source, seed_conversion_job, seed_conversion_output, "ctx_backfill")
    db_path = _db_path(graph_db_url)
    assert not _competing_writer_is_locked_out(db_path), "precondition: the write lock starts free"

    with _capacity_probe(monkeypatch, db_path) as observed:
        run_contextualization_backfill(
            migrated_engine,
            output_ids=None,
            limit=None,
            confirmed=True,
            dry_run=False,
            queue_max_depth=_LIMIT,
        )

    _assert_evaluated_under_the_write_lock(observed)


def test_the_extraction_backfill_evaluates_capacity_under_its_write_lock(
    migrated_engine: Engine,
    db_session: Session,
    graph_db_url: str,
    seed_source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corpus-wide extraction backfill counts inside its own transaction."""
    _seed_chunked(db_session, seed_source, "ext_backfill")
    db_path = _db_path(graph_db_url)
    assert not _competing_writer_is_locked_out(db_path), "precondition: the write lock starts free"

    with _capacity_probe(monkeypatch, db_path) as observed:
        run_extraction_backfill(
            migrated_engine,
            source_ids=None,
            confirmed=True,
            dry_run=False,
            queue_max_depth=_LIMIT,
        )

    _assert_evaluated_under_the_write_lock(observed)


def test_the_contextualization_intake_evaluates_capacity_under_its_write_lock(
    migrated_engine: Engine,
    db_session: Session,
    graph_db_url: str,
    seed_source,
    seed_conversion_job,
    seed_conversion_output,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contextualization intake route counts inside the transaction it commits."""
    output_id = _seed_converted(db_session, seed_source, seed_conversion_job, seed_conversion_output, "ctx_intake")
    monkeypatch.setenv("AIZK_GRAPH__CONTEXTUALIZATION_QUEUE_MAX_DEPTH", str(_LIMIT))
    db_path = _db_path(graph_db_url)
    assert not _competing_writer_is_locked_out(db_path), "precondition: the write lock starts free"

    with _capacity_probe(monkeypatch, db_path) as observed, TestClient(create_app()) as client:
        response = client.post("/v1/contextualizations", json={"conversion_output_id": output_id})

    assert response.status_code == 201
    _assert_evaluated_under_the_write_lock(observed)


def test_the_extraction_intake_evaluates_capacity_under_its_write_lock(
    migrated_engine: Engine,
    db_session: Session,
    graph_db_url: str,
    seed_source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extraction intake route counts inside the transaction it commits."""
    source_id = _seed_chunked(db_session, seed_source, "ext_intake")
    monkeypatch.setenv("AIZK_GRAPH__EXTRACTION_QUEUE_MAX_DEPTH", str(_LIMIT))
    db_path = _db_path(graph_db_url)
    assert not _competing_writer_is_locked_out(db_path), "precondition: the write lock starts free"

    with _capacity_probe(monkeypatch, db_path) as observed, TestClient(create_app()) as client:
        response = client.post("/v1/extractions", json={"source_id": str(source_id)})

    assert response.status_code == 201
    _assert_evaluated_under_the_write_lock(observed)
