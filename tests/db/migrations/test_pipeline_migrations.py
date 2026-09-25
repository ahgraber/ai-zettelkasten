"""Behavioral tests for the pipeline_runs / pipeline_events tables in the migrated schema.

Structural equivalence to the ORM baseline (columns, nullability, indexes,
foreign keys, unique constraints) is covered once, across every table, by
``tests/db/migrations/test_baseline.py``. This module exercises pipeline-table
behavior that only a live insert or ORM round-trip can prove: the partial
unique index's predicate, and event round-trip queryability.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.run import PipelineRun

_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent
_BASELINE_MIGRATION = _MIGRATIONS_DIR / "versions" / "a62f1bf47735_baseline.py"


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def test_pipeline_active_run_partial_unique_index_present(tmp_path: Path) -> None:
    """The migrated schema carries the partial unique index enforcing one active run."""
    url = f"sqlite:///{tmp_path / 'idx.db'}"
    command.upgrade(_alembic_cfg(url), "head")

    indexes = {i["name"]: i for i in inspect(create_engine(url)).get_indexes("pipeline_runs")}
    active_idx = indexes.get("uq_pipeline_runs_active_scope")
    assert active_idx is not None, "partial unique index missing"
    assert bool(active_idx["unique"])
    assert sorted(active_idx["column_names"]) == ["scope_id", "stage"]


def test_pipeline_active_run_index_keeps_postgres_predicate() -> None:
    """The active-run index predicate is declared for SQLite and Postgres."""
    active_idx = next(i for i in PipelineRun.__table__.indexes if i.name == "uq_pipeline_runs_active_scope")
    assert str(active_idx.dialect_options["sqlite"]["where"]) == "status = 'active'"
    assert str(active_idx.dialect_options["postgresql"]["where"]) == "status = 'active'"

    migration_text = _BASELINE_MIGRATION.read_text()
    assert "sqlite_where=sa.text(\"status = 'active'\")" in migration_text
    assert "postgresql_where=sa.text(\"status = 'active'\")" in migration_text


def _insert_run(conn, *, status: str, derivation_key: str) -> None:
    """Insert a pipeline_runs row for the fixed scope directly (bypassing record_run)."""
    conn.execute(
        text(
            "INSERT INTO pipeline_runs (stage, scope_id, status, derivation_key,"
            " version_stamps_json, created_at)"
            " VALUES ('teststage', 'scope', :status, :fp, '{}', '2026-01-01T00:00:00')"
        ),
        {"status": status, "fp": derivation_key},
    )


def test_migrated_index_is_partial_not_full(tmp_path: Path) -> None:
    """The migrated index admits many superseded runs but only one active run per scope.

    A full unique index on (stage, scope_id) would pass the structural check
    above yet reject a second superseded run — breaking the supersession model.
    Exercising the predicate is the only way to tell a partial index from a full
    one through the inspector, which reports both as ``unique``.
    """
    url = f"sqlite:///{tmp_path / 'predicate.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    # Multiple superseded runs for one scope coexist — the predicate exempts them.
    with engine.connect() as conn:
        _insert_run(conn, status="superseded", derivation_key="key-1")
        _insert_run(conn, status="superseded", derivation_key="key-2")
        _insert_run(conn, status="active", derivation_key="key-3")
        conn.commit()

    # A second active run for the same scope violates the partial unique index.
    with pytest.raises(IntegrityError), engine.connect() as conn:
        _insert_run(conn, status="active", derivation_key="key-4")
        conn.commit()


def test_migrated_schema_supports_event_orm_round_trip(tmp_path: Path) -> None:
    """A PipelineEvent inserts and is queryable by source_id against the migrated schema."""
    url = f"sqlite:///{tmp_path / 'events.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    source = uuid4()
    with Session(engine) as session:
        session.add(
            PipelineEvent(
                stage="conversion",
                work_unit_ref="job:1",
                source_id=source,
                from_status=None,
                to_status="running",
                kind="origin",
                payload_json="{}",
            )
        )
        session.commit()

    with Session(engine) as session:
        rows = list(session.exec(select(PipelineEvent).where(PipelineEvent.source_id == source)))
        assert len(rows) == 1, "event is queryable by its source identity"
        assert rows[0].source_id == source
