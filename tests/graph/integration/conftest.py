"""Integration-test harness for the graph operator UI.

Drives the real graph operator FastAPI app (``aizk.graph.api.main.create_app``)
over a per-test SQLite database built by the **conversion Alembic migrations**, so
every table the UI joins exists (conversion ``sources`` / ``conversion_jobs`` /
``conversion_outputs``, the ``graph_*`` tables, and the shared
``pipeline_runs`` / ``pipeline_events``) and the database is forward-compatible
with later migrations. The app resolves its database URL and trusted-host
allowlist from the process environment; the harness sets only those two variables
(via ``monkeypatch``) so the run stays hermetic and never reads ``.env``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.db.engine import _ENGINE_CACHE, get_engine
from aizk.db.migrations import run_migrations
from aizk.graph.api.dependencies import get_blob_reader
from aizk.graph.api.main import create_app
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.mention_store import extraction_derivation_key
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.run import PipelineRun, RunStatus


@pytest.fixture
def graph_db_url(tmp_path: Path) -> str:
    """Return a file-based SQLite URL for a per-test graph operator database."""
    return f"sqlite:///{tmp_path / 'graph_ui.db'}"


@pytest.fixture
def _graph_ui_env(monkeypatch: pytest.MonkeyPatch, graph_db_url: str) -> None:
    """Point the app at the test database and allow the TestClient default host.

    The trusted-host allowlist is read by ``create_app`` from the environment, and
    ``TestClient`` sends ``Host: testserver`` by default, so ``testserver`` must be
    allowed alongside the loopback defaults for the normal-path requests.
    """
    monkeypatch.setenv("AIZK_DATABASE_URL", graph_db_url)
    monkeypatch.setenv("AIZK_TRUSTED_HOSTS", '["testserver", "localhost", "127.0.0.1"]')


@pytest.fixture
def migrated_engine(graph_db_url: str, _graph_ui_env: None) -> Iterator[Engine]:
    """A migration-built engine on the test database, shared with the app via cache.

    Disposes the engine and drops it from ``_ENGINE_CACHE`` at teardown so SQLite
    connection-pool file handles do not accumulate across the session.
    """
    run_migrations(graph_db_url)
    engine = get_engine(graph_db_url)
    try:
        yield engine
    finally:
        engine.dispose()
        _ENGINE_CACHE.pop(graph_db_url, None)


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """An open session on the migration-built test database for seeding rows."""
    with Session(migrated_engine) as session:
        yield session


@pytest.fixture
def app(migrated_engine: Engine) -> FastAPI:
    """The graph operator app, built after the environment and database are ready."""
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A TestClient over the real graph operator app (no dependency overrides)."""
    with TestClient(app) as test_client:
        yield test_client


class FakeBlobReader:
    """An in-memory :class:`~aizk.graph.markdown_source.BlobReader` for the explorer.

    Maps a storage key to canned bytes, so the explorer's on-demand markdown
    reconstruction (``S3MarkdownSource(engine, blob_reader).load``) is exercised
    against a seeded ``ConversionOutput`` row without reaching real S3. An unknown
    key raises ``KeyError`` (a programming error in the test, not a degrade path).
    """

    def __init__(self, blobs: dict[str, bytes]) -> None:
        """Store the key → bytes mapping the fake serves."""
        self._blobs = blobs

    def get_object_bytes(self, s3_key: str) -> bytes:
        """Return the canned bytes for ``s3_key``."""
        return self._blobs[s3_key]


@pytest.fixture
def explorer_markdown() -> str:
    """The canned source markdown the fake blob reader serves to the explorer."""
    return "# Attention\n\nThe reconstructed source markdown body for the explorer detail panel."


@pytest.fixture
def explorer_client(app: FastAPI, explorer_markdown: str) -> Iterator[TestClient]:
    """A TestClient whose ``get_blob_reader`` is overridden with a fake reader.

    The fake serves ``explorer_markdown`` under the ``markdown_key`` the seeded
    ``ConversionOutput`` records (the default ``"markdown.md"`` from the
    ``seed_conversion_output`` factory), so the detail panel's markdown
    reconstruction resolves the row and reads the canned bytes.
    """
    fake = FakeBlobReader({"markdown.md": explorer_markdown.encode("utf-8")})
    app.dependency_overrides[get_blob_reader] = lambda: fake
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_blob_reader, None)


@pytest.fixture
def seed_conversion_job() -> "Callable[..., ConversionJob]":
    """Return a factory that inserts a :class:`ConversionJob` and returns the refreshed row.

    A conversion output needs a job to hang off, so a test that seeds an output
    seeds one of these first.
    """

    def _make(
        session: Session,
        *,
        source_id: UUID,
        idempotency_key: str,
        status: ConversionJobStatus,
        owner_id: str = "self",
        title: str = "Doc",
        attempts: int = 0,
    ) -> ConversionJob:
        job = ConversionJob(
            source_id=source_id,
            owner_id=owner_id,
            title=title,
            payload_version=1,
            status=status,
            attempts=attempts,
            idempotency_key=idempotency_key,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job

    return _make


@pytest.fixture
def seed_extraction_state() -> "Callable[..., None]":
    """Return a factory that puts a source in a current or stale extraction state.

    Both runs are activated for the source: a chunking run, and an extraction run
    recording the upstream key it consumed. With ``current=False`` that recorded key
    no longer matches the chunking run, which is what makes the source stale.
    """

    def _make(session: Session, *, source_id: UUID, current: bool) -> None:
        consumed = "chunking-current" if current else "chunking-superseded"
        session.add(
            PipelineRun(
                stage=CHUNKING_STAGE,
                scope_id=str(source_id),
                status=RunStatus.ACTIVE,
                derivation_key="chunking-current",
            )
        )
        session.add(
            PipelineRun(
                stage=EXTRACTION_STAGE,
                scope_id=str(source_id),
                status=RunStatus.ACTIVE,
                derivation_key=extraction_derivation_key(
                    extractor_version="stub/v1",
                    materializer_version="v1",
                    input_policy="raw",
                    upstream_derivation_key=consumed,
                ),
                version_stamps_json='{"input_policy":"raw"}',
            )
        )
        session.commit()

    return _make
