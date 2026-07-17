"""Shared fixtures for the graph-stage tests.

Builds file-based SQLite engines holding the graph tables (``graph_*``) plus the
shared ``pipeline_runs`` / ``pipeline_events`` tables the graph stage records
runs and transitions on, and the conversion ``sources`` / ``conversion_jobs`` /
``conversion_outputs`` tables the operator UI joins (the jobs table enriches a
work-unit with its ``Source.title``; the explorer resolves markdown through a
``ConversionOutput`` locator). The ``serialized_session`` fixture mirrors
production's single serialized writer: every transaction starts with
``BEGIN IMMEDIATE`` so concurrent writers acquire the write lock up front rather
than deadlocking on a lock upgrade. Tests never touch the conversion config or
``.env``.

The ``seed_source`` / ``seed_conversion_output`` / ``seed_contextualization_job``
fixtures return session-agnostic factory callables shared across the graph
operator-UI suites so a row can be seeded against either the lightweight
``create_all`` engine or the migration-backed integration engine.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, event, text
from sqlmodel import Session, SQLModel, create_engine

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.graph.content_index import CONTENT_FTS_DDL
from aizk.graph.datamodel import (
    Chunk,
    ChunkRunInput,
    ChunkRunManifest,
    ContextualizationJob,
    ContextualizationOutputMemo,
    ContextualizedChunk,
    DocumentSummary,
    ExtractionJob,
)
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun

_GRAPH_SCHEMA_TABLES = [
    Chunk.__table__,
    ChunkRunInput.__table__,
    ChunkRunManifest.__table__,
    DocumentSummary.__table__,
    ContextualizedChunk.__table__,
    ContextualizationOutputMemo.__table__,
    ContextualizationJob.__table__,
    ExtractionJob.__table__,
    PipelineRun.__table__,
    PipelineEvent.__table__,
    # Conversion tables the operator UI reads alongside the graph tables. Ordered
    # after the graph tables; SQLite does not require an FK target to pre-exist at
    # CREATE time, so the conversion-internal FK web (conversion_outputs -> sources
    # / conversion_jobs) does not constrain ordering here.
    Source.__table__,
    ConversionJob.__table__,
    ConversionOutput.__table__,
]


def create_content_fts(engine: Engine) -> None:
    """Create the ``graph_content_fts`` virtual table on ``engine``.

    ``SQLModel.metadata.create_all`` cannot create an FTS5 virtual table, so any
    test engine the persist path writes index rows into must call this after
    ``create_all``. Uses the migration's DDL (imported via
    :data:`aizk.graph.content_index.CONTENT_FTS_DDL`) as the single source of truth.
    """
    with engine.begin() as conn:
        conn.execute(text(CONTENT_FTS_DDL))


def _create_graph_schema(url: str) -> None:
    """Create the graph and pipeline tables on ``url`` using a throwaway engine.

    Also creates the ``graph_content_fts`` virtual table after the SQLModel tables,
    since the persist path writes index rows into it.
    """
    setup_engine = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(setup_engine, tables=_GRAPH_SCHEMA_TABLES)
    create_content_fts(setup_engine)
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


@pytest.fixture
def seed_source() -> Callable[..., Source]:
    """Return a factory that inserts a :class:`Source` and returns the refreshed row.

    The factory derives the required ``source_ref`` / ``source_ref_hash`` from a
    KaraKeep bookmark reference so callers only supply a ``karakeep_id`` and the
    optional enriched ``title``.
    """

    def _make(
        session: Session,
        *,
        karakeep_id: str,
        title: str | None = None,
        source_id: UUID | None = None,
        owner_id: str = "self",
    ) -> Source:
        ref = KarakeepBookmarkRef(bookmark_id=karakeep_id)
        fields: dict[str, object] = {
            "owner_id": owner_id,
            "karakeep_id": karakeep_id,
            "source_ref": ref.model_dump_json(),
            "source_ref_hash": compute_source_ref_hash(ref),
            "title": title,
            "url": f"https://example.com/{karakeep_id}",
            "normalized_url": f"https://example.com/{karakeep_id}",
            "content_type": "html",
            "source_type": "other",
        }
        if source_id is not None:
            fields["source_id"] = source_id
        source = Source(**fields)
        session.add(source)
        session.commit()
        session.refresh(source)
        return source

    return _make


@pytest.fixture
def seed_conversion_output() -> Callable[..., ConversionOutput]:
    """Return a factory that inserts a :class:`ConversionOutput` markdown locator."""

    def _make(
        session: Session,
        *,
        job_id: int,
        source_id: UUID,
        title: str = "Untitled",
        owner_id: str = "self",
        markdown_hash_xx64: str = "0" * 16,
        s3_prefix: str = "graph/test",
        markdown_key: str = "markdown.md",
        manifest_key: str = "manifest.json",
    ) -> ConversionOutput:
        output = ConversionOutput(
            job_id=job_id,
            source_id=source_id,
            owner_id=owner_id,
            title=title,
            payload_version=1,
            s3_prefix=s3_prefix,
            markdown_key=markdown_key,
            manifest_key=manifest_key,
            markdown_hash_xx64=markdown_hash_xx64,
            docling_version="0.0.0",
            pipeline_name="test",
        )
        session.add(output)
        session.commit()
        session.refresh(output)
        return output

    return _make


@pytest.fixture
def seed_contextualization_job() -> Callable[..., ContextualizationJob]:
    """Return a factory that inserts a :class:`ContextualizationJob` work-unit."""

    def _make(
        session: Session,
        *,
        source_id: UUID,
        conversion_output_id: int = 1,
        status: WorkUnitStatus = WorkUnitStatus.QUEUED,
        attempts: int = 0,
        idempotency_key: str | None = None,
    ) -> ContextualizationJob:
        job = ContextualizationJob(
            idempotency_key=idempotency_key or f"conversion_output:{conversion_output_id}",
            conversion_output_id=conversion_output_id,
            source_id=source_id,
            status=status,
            attempts=attempts,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job

    return _make


@pytest.fixture
def seed_extraction_job() -> Callable[..., ExtractionJob]:
    """Return a factory that inserts an :class:`ExtractionJob` work-unit."""

    def _make(
        session: Session,
        *,
        source_id: UUID,
        status: WorkUnitStatus = WorkUnitStatus.QUEUED,
        attempts: int = 0,
        idempotency_key: str | None = None,
    ) -> ExtractionJob:
        job = ExtractionJob(
            idempotency_key=idempotency_key or f"source:{source_id}",
            source_id=source_id,
            status=status,
            attempts=attempts,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job

    return _make
