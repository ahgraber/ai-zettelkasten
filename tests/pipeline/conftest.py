"""Shared fixtures for the pipeline-runtime primitive tests.

The fixtures build file-based SQLite engines holding only the pipeline tables
(``pipeline_runs`` / ``pipeline_events``), so these tests never depend on the
conversion schema or its config. The ``serialized_engine`` fixture mirrors
production's single serialized writer: it issues ``BEGIN IMMEDIATE`` at the
start of every transaction so concurrent writers acquire the write lock up front
rather than deadlocking on a shared-to-reserved lock upgrade.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlmodel import SQLModel, create_engine

from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.run import PipelineRun

_PIPELINE_TABLES = [PipelineRun.__table__, PipelineEvent.__table__]


def _create_pipeline_schema(url: str) -> None:
    """Create only the pipeline tables on ``url`` using a throwaway plain engine."""
    setup_engine = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(setup_engine, tables=_PIPELINE_TABLES)
    setup_engine.dispose()


def _attach_begin_immediate(engine: Engine) -> None:
    """Make every transaction on ``engine`` start with ``BEGIN IMMEDIATE``."""

    @event.listens_for(engine, "connect")
    def _disable_implicit_begin(dbapi_connection, _record) -> None:  # noqa: ANN001
        # Hand transaction control to SQLAlchemy so the "begin" hook can emit
        # BEGIN IMMEDIATE instead of pysqlite's implicit deferred BEGIN.
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_begin_immediate(connection) -> None:  # noqa: ANN001
        connection.exec_driver_sql("BEGIN IMMEDIATE")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """Return a file-based SQLite URL for a per-test database."""
    return f"sqlite:///{tmp_path / 'pipeline.db'}"


@pytest.fixture
def engine(db_url: str) -> Engine:
    """A plain SQLite engine with the pipeline tables created."""
    _create_pipeline_schema(db_url)
    eng = create_engine(db_url, connect_args={"check_same_thread": False})
    yield eng
    eng.dispose()


@pytest.fixture
def serialized_engine(db_url: str) -> Engine:
    """A SQLite engine that serializes writers via ``BEGIN IMMEDIATE`` + busy-timeout."""
    _create_pipeline_schema(db_url)
    eng = create_engine(db_url, connect_args={"check_same_thread": False, "timeout": 30})
    _attach_begin_immediate(eng)
    yield eng
    eng.dispose()
