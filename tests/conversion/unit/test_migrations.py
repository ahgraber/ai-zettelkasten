"""Tests for Alembic migration integrity.

Covers the `schema-migrations` spec contracts.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlmodel import SQLModel, create_engine, inspect

import aizk.conversion.datamodel  # noqa: F401
from aizk.conversion.migrations import run_migrations


def _alembic_cfg(database_url: str) -> Config:
    """Return an Alembic config pointing at the given database."""
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[3] / "src" / "aizk" / "conversion" / "migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _normalize_index(idx: dict) -> tuple:
    """Return a comparable key for an index."""
    return (idx["name"], tuple(sorted(idx["column_names"])), idx.get("unique", False))


def _normalize_fk(fk: dict) -> tuple:
    """Return a comparable key for a foreign key."""
    return (
        tuple(sorted(fk["constrained_columns"])),
        fk["referred_table"],
        tuple(sorted(fk["referred_columns"])),
    )


def test_upgrade_produces_schema_matching_create_all(tmp_path):
    """Verify that running all migrations produces the same schema as create_all."""
    migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    baseline_url = f"sqlite:///{tmp_path / 'baseline.db'}"

    # Schema via migrations
    run_migrations(migrated_url)

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
        # Column names
        baseline_cols = {c["name"] for c in baseline_inspector.get_columns(table)}
        migrated_cols = {c["name"] for c in migrated_inspector.get_columns(table)}
        assert baseline_cols == migrated_cols, f"{table} column mismatch: {baseline_cols ^ migrated_cols}"

        # Nullable
        baseline_nullable = {c["name"]: c["nullable"] for c in baseline_inspector.get_columns(table)}
        migrated_nullable = {c["name"]: c["nullable"] for c in migrated_inspector.get_columns(table)}
        for col in baseline_cols:
            assert baseline_nullable[col] == migrated_nullable[col], (
                f"{table}.{col} nullable mismatch: "
                f"baseline={baseline_nullable[col]}, migrated={migrated_nullable[col]}"
            )

        # Indexes
        baseline_indexes = {_normalize_index(i) for i in baseline_inspector.get_indexes(table)}
        migrated_indexes = {_normalize_index(i) for i in migrated_inspector.get_indexes(table)}
        assert baseline_indexes == migrated_indexes, (
            f"{table} index mismatch:\n"
            f"  only in baseline: {baseline_indexes - migrated_indexes}\n"
            f"  only in migrated: {migrated_indexes - baseline_indexes}"
        )

        # Foreign keys
        baseline_fks = {_normalize_fk(fk) for fk in baseline_inspector.get_foreign_keys(table)}
        migrated_fks = {_normalize_fk(fk) for fk in migrated_inspector.get_foreign_keys(table)}
        assert baseline_fks == migrated_fks, (
            f"{table} foreign key mismatch:\n"
            f"  only in baseline: {baseline_fks - migrated_fks}\n"
            f"  only in migrated: {migrated_fks - baseline_fks}"
        )

        # Unique constraints
        baseline_uniques = {
            tuple(sorted(uc["column_names"])) for uc in baseline_inspector.get_unique_constraints(table)
        }
        migrated_uniques = {
            tuple(sorted(uc["column_names"])) for uc in migrated_inspector.get_unique_constraints(table)
        }
        assert baseline_uniques == migrated_uniques, (
            f"{table} unique constraint mismatch:\n"
            f"  only in baseline: {baseline_uniques - migrated_uniques}\n"
            f"  only in migrated: {migrated_uniques - baseline_uniques}"
        )


def test_upgrade_downgrade_round_trip(tmp_path):
    """Verify that upgrade then downgrade leaves no tables behind."""
    db_url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _alembic_cfg(db_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    inspector = inspect(create_engine(db_url))
    remaining = set(inspector.get_table_names()) - {"alembic_version"}
    assert remaining == set(), f"Tables remain after downgrade: {remaining}"
