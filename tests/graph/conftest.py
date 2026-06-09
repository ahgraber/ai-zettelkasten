"""Shared fixtures for the graph-stage tests.

Builds file-based SQLite engines holding the graph tables (``graph_*``) plus the
shared ``pipeline_runs`` / ``pipeline_events`` tables the graph stage records
runs and transitions on. The ``serialized_session`` fixture mirrors production's
single serialized writer: every transaction starts with ``BEGIN IMMEDIATE`` so
concurrent writers acquire the write lock up front rather than deadlocking on a
lock upgrade. Tests never touch the conversion config or ``.env``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from aizk.graph.datamodel import (
    Chunk,
    ChunkRunInput,
    ChunkRunManifest,
    ContextualizationJob,
    ContextualizationOutputMemo,
    ContextualizedChunk,
    DocumentSummary,
)
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.run import PipelineRun

_GRAPH_SCHEMA_TABLES = [
    Chunk.__table__,
    ChunkRunInput.__table__,
    ChunkRunManifest.__table__,
    DocumentSummary.__table__,
    ContextualizedChunk.__table__,
    ContextualizationOutputMemo.__table__,
    ContextualizationJob.__table__,
    PipelineRun.__table__,
    PipelineEvent.__table__,
]


def _create_graph_schema(url: str) -> None:
    """Create the graph and pipeline tables on ``url`` using a throwaway engine."""
    setup_engine = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(setup_engine, tables=_GRAPH_SCHEMA_TABLES)
    setup_engine.dispose()


def _attach_begin_immediate(engine: Engine) -> None:
    """Make every transaction on ``engine`` start with ``BEGIN IMMEDIATE``."""

    @event.listens_for(engine, "connect")
    def _disable_implicit_begin(dbapi_connection, _record) -> None:  # noqa: ANN001
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_begin_immediate(connection) -> None:  # noqa: ANN001
        connection.exec_driver_sql("BEGIN IMMEDIATE")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """Return a file-based SQLite URL for a per-test database."""
    return f"sqlite:///{tmp_path / 'graph.db'}"


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    """A SQLite engine with the graph + pipeline tables, serialized via BEGIN IMMEDIATE."""
    _create_graph_schema(db_url)
    eng = create_engine(db_url, connect_args={"check_same_thread": False, "timeout": 30})
    _attach_begin_immediate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """An open session on the graph engine; the test owns commit boundaries."""
    with Session(engine) as sess:
        yield sess
