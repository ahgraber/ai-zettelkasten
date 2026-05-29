"""Schema-fidelity tests for the pipeline_runs / pipeline_events migration.

Asserts the migrated schema for the two pipeline tables is structurally
equivalent to the ORM baseline (``create_all``), and that the revision
downgrades cleanly without disturbing the conversion tables. Scoped to the
pipeline tables so it is independent of any models other test modules register
on ``SQLModel.metadata``; the full cross-table parity check lives in the
conversion migration suite.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.run import PipelineRun

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "aizk" / "conversion" / "migrations"
_PIPELINE_MIGRATION = _MIGRATIONS_DIR / "versions" / "d0e1f2a3b4c5_add_pipeline_runs_and_events.py"

_PIPELINE_REVISION = "d0e1f2a3b4c5"
_PREV_REVISION = "a8c9d0e1f2b3"
_PIPELINE_TABLES = ("pipeline_runs", "pipeline_events")


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _normalize_index(idx: dict) -> tuple:
    return (idx["name"], tuple(sorted(idx["column_names"])), bool(idx.get("unique", False)))


def _normalize_fk(fk: dict) -> tuple:
    return (
        tuple(sorted(fk["constrained_columns"])),
        fk["referred_table"],
        tuple(sorted(fk["referred_columns"])),
    )


def test_pipeline_tables_match_create_all(tmp_path: Path) -> None:
    """Running migrations produces the same pipeline-table schema as create_all."""
    migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    baseline_url = f"sqlite:///{tmp_path / 'baseline.db'}"

    command.upgrade(_alembic_cfg(migrated_url), "head")

    baseline_engine = create_engine(baseline_url)
    SQLModel.metadata.create_all(
        baseline_engine,
        tables=[PipelineRun.__table__, PipelineEvent.__table__],
    )

    migrated = inspect(create_engine(migrated_url))
    baseline = inspect(baseline_engine)

    migrated_tables = set(migrated.get_table_names())
    assert set(_PIPELINE_TABLES) <= migrated_tables, f"missing pipeline tables: {migrated_tables}"

    for table in _PIPELINE_TABLES:
        baseline_cols = {c["name"]: c["nullable"] for c in baseline.get_columns(table)}
        migrated_cols = {c["name"]: c["nullable"] for c in migrated.get_columns(table)}
        assert migrated_cols == baseline_cols, f"{table} column/nullable mismatch"

        assert {_normalize_index(i) for i in migrated.get_indexes(table)} == {
            _normalize_index(i) for i in baseline.get_indexes(table)
        }, f"{table} index mismatch"

        assert {_normalize_fk(fk) for fk in migrated.get_foreign_keys(table)} == {
            _normalize_fk(fk) for fk in baseline.get_foreign_keys(table)
        }, f"{table} foreign key mismatch"

        baseline_uniques = {tuple(sorted(uc["column_names"])) for uc in baseline.get_unique_constraints(table)}
        migrated_uniques = {tuple(sorted(uc["column_names"])) for uc in migrated.get_unique_constraints(table)}
        assert migrated_uniques == baseline_uniques, f"{table} unique constraint mismatch"


def test_pipeline_active_run_partial_unique_index_present(tmp_path: Path) -> None:
    """The migrated schema carries the partial unique index enforcing one active run."""
    url = f"sqlite:///{tmp_path / 'idx.db'}"
    command.upgrade(_alembic_cfg(url), "head")

    indexes = {i["name"]: i for i in inspect(create_engine(url)).get_indexes("pipeline_runs")}
    active_idx = indexes.get("uq_pipeline_runs_active_scope")
    assert active_idx is not None, "partial unique index missing"
    assert bool(active_idx["unique"])
    assert sorted(active_idx["column_names"]) == ["scope_key", "stage"]


def test_pipeline_active_run_index_keeps_postgres_predicate() -> None:
    """The active-run index predicate is declared for SQLite and Postgres."""
    active_idx = next(i for i in PipelineRun.__table__.indexes if i.name == "uq_pipeline_runs_active_scope")
    assert str(active_idx.dialect_options["sqlite"]["where"]) == "status = 'active'"
    assert str(active_idx.dialect_options["postgresql"]["where"]) == "status = 'active'"

    migration_text = _PIPELINE_MIGRATION.read_text()
    assert "sqlite_where=sa.text(\"status = 'active'\")" in migration_text
    assert "postgresql_where=sa.text(\"status = 'active'\")" in migration_text


def _insert_run(conn, *, status: str, fingerprint: str) -> None:
    """Insert a pipeline_runs row for the fixed scope directly (bypassing record_run)."""
    conn.execute(
        text(
            "INSERT INTO pipeline_runs (stage, scope_key, status, input_fingerprint,"
            " version_stamps_json, created_at)"
            " VALUES ('teststage', 'scope', :status, :fp, '{}', '2026-01-01T00:00:00')"
        ),
        {"status": status, "fp": fingerprint},
    )


def test_migrated_index_is_partial_not_full(tmp_path: Path) -> None:
    """The migrated index admits many superseded runs but only one active run per scope.

    A full unique index on (stage, scope_key) would pass the structural check
    above yet reject a second superseded run — breaking the supersession model.
    Exercising the predicate is the only way to tell a partial index from a full
    one through the inspector, which reports both as ``unique``.
    """
    url = f"sqlite:///{tmp_path / 'predicate.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    # Multiple superseded runs for one scope coexist — the predicate exempts them.
    with engine.connect() as conn:
        _insert_run(conn, status="superseded", fingerprint="fp-1")
        _insert_run(conn, status="superseded", fingerprint="fp-2")
        _insert_run(conn, status="active", fingerprint="fp-3")
        conn.commit()

    # A second active run for the same scope violates the partial unique index.
    with pytest.raises(IntegrityError), engine.connect() as conn:
        _insert_run(conn, status="active", fingerprint="fp-4")
        conn.commit()


def test_migrated_schema_supports_event_orm_round_trip(tmp_path: Path) -> None:
    """A PipelineEvent inserts and is queryable by aizk_uuid against the migrated schema."""
    url = f"sqlite:///{tmp_path / 'events.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    source = uuid4()
    with Session(engine) as session:
        session.add(
            PipelineEvent(
                stage="conversion",
                work_unit_ref="job:1",
                aizk_uuid=source,
                from_status=None,
                to_status="running",
                kind="origin",
                payload_json="{}",
            )
        )
        session.commit()

    with Session(engine) as session:
        rows = list(session.exec(select(PipelineEvent).where(PipelineEvent.aizk_uuid == source)))
        assert len(rows) == 1, "event is queryable by its source identity"
        assert rows[0].aizk_uuid == source


def test_pipeline_revision_downgrade_drops_only_pipeline_tables(tmp_path: Path) -> None:
    """Downgrading one revision drops the pipeline tables and leaves conversion tables."""
    url = f"sqlite:///{tmp_path / 'down.db'}"
    cfg = _alembic_cfg(url)

    command.upgrade(cfg, "head")
    tables_at_head = set(inspect(create_engine(url)).get_table_names())
    assert set(_PIPELINE_TABLES) <= tables_at_head

    command.downgrade(cfg, _PREV_REVISION)
    tables_after = set(inspect(create_engine(url)).get_table_names())

    assert not (set(_PIPELINE_TABLES) & tables_after), "pipeline tables should be dropped"
    assert "conversion_jobs" in tables_after, "conversion tables remain after downgrade"
    assert "conversion_job_events" in tables_after
