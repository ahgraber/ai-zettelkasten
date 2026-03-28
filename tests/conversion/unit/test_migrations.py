"""Tests for Alembic migration integrity."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlmodel import SQLModel, create_engine, inspect

import aizk.conversion.datamodel  # noqa: F401


def _alembic_cfg(database_url: str) -> Config:
    """Return an Alembic config pointing at the given database."""
    repo_root = Path(__file__).resolve().parents[3]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def test_upgrade_produces_schema_matching_create_all(tmp_path):
    """Verify that running all migrations produces the same schema as create_all."""
    migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    baseline_url = f"sqlite:///{tmp_path / 'baseline.db'}"

    # Schema via migrations
    command.upgrade(_alembic_cfg(migrated_url), "head")

    # Schema via create_all
    baseline_engine = create_engine(baseline_url)
    SQLModel.metadata.create_all(baseline_engine)

    migrated_inspector = inspect(create_engine(migrated_url))
    baseline_inspector = inspect(baseline_engine)

    migrated_tables = set(migrated_inspector.get_table_names())
    baseline_tables = set(baseline_inspector.get_table_names())
    # Alembic adds alembic_version; filter it out
    migrated_tables.discard("alembic_version")

    assert migrated_tables == baseline_tables, f"Table mismatch: {migrated_tables ^ baseline_tables}"

    for table in baseline_tables:
        baseline_cols = {c["name"] for c in baseline_inspector.get_columns(table)}
        migrated_cols = {c["name"] for c in migrated_inspector.get_columns(table)}
        assert baseline_cols == migrated_cols, f"{table} column mismatch: {baseline_cols ^ migrated_cols}"


def test_upgrade_downgrade_round_trip(tmp_path):
    """Verify that upgrade then downgrade leaves no tables behind."""
    db_url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _alembic_cfg(db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    inspector = inspect(create_engine(db_url))
    remaining = set(inspector.get_table_names()) - {"alembic_version"}
    assert remaining == set(), f"Tables remain after downgrade: {remaining}"
