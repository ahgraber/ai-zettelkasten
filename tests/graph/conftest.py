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

from collections.abc import Callable
from uuid import UUID

import pytest
from sqlmodel import Session

from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.pipeline.lifecycle import WorkUnitStatus
from tests._graphdb import (
    _GRAPH_SCHEMA_TABLES,
    _attach_begin_immediate,
    _create_graph_schema,
    create_content_fts,
    db_url,
    engine,
    seed_conversion_output,
    seed_source,
    session as session,
)


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
