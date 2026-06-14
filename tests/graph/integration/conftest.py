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

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aizk.conversion.db import _ENGINE_CACHE, get_engine
from aizk.conversion.migrations import run_migrations
from aizk.graph.api.main import create_app


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
