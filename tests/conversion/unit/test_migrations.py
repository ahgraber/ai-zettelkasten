"""Tests for Alembic migration integrity.

Covers the `schema-migrations` spec contracts.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlmodel import SQLModel, create_engine, inspect

from aizk.conversion.core.errors import IrreversibleMigrationError
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


# --- d5e6f7a8b9c0 enforce_source_ref_not_null ----------------------------------


def test_enforce_not_null_aborts_with_null_source_ref(tmp_path):
    """Pre-flight abort: upgrade raises IrreversibleMigrationError when source_ref IS NULL."""
    import datetime
    import uuid

    db_url = f"sqlite:///{tmp_path / 'abort.db'}"
    cfg = _alembic_cfg(db_url)

    command.upgrade(cfg, "c1d2e3f4a5b6")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO sources "
                "(karakeep_id, aizk_uuid, source_ref, source_ref_hash, "
                "url, normalized_url, title, content_type, source_type, "
                "created_at, updated_at) "
                "VALUES (:kid, :uuid, NULL, NULL, NULL, NULL, NULL, NULL, NULL, :now, :now)"
            ),
            {
                "kid": "test-null-ref",
                "uuid": str(uuid.uuid4()),
                "now": datetime.datetime.now(datetime.UTC).isoformat(),
            },
        )

    with pytest.raises(IrreversibleMigrationError, match="NULL source_ref or source_ref_hash"):
        command.upgrade(cfg, "d5e6f7a8b9c0")


def test_enforce_not_null_round_trip_on_populated_database(tmp_path):
    """Round-trip upgrade/downgrade of d5e6f7a8b9c0 on a fully-populated database."""
    import datetime
    import hashlib
    import json
    import uuid

    db_url = f"sqlite:///{tmp_path / 'populated.db'}"
    cfg = _alembic_cfg(db_url)

    command.upgrade(cfg, "c1d2e3f4a5b6")

    payload = {"bookmark_id": "abc123", "kind": "karakeep_bookmark"}
    source_ref = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    source_ref_hash = hashlib.sha256(source_ref.encode()).hexdigest()

    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO sources "
                "(karakeep_id, aizk_uuid, source_ref, source_ref_hash, "
                "url, normalized_url, title, content_type, source_type, "
                "created_at, updated_at) "
                "VALUES (:kid, :uuid, :ref, :hash, NULL, NULL, NULL, NULL, NULL, :now, :now)"
            ),
            {
                "kid": "abc123",
                "uuid": str(uuid.uuid4()),
                "ref": source_ref,
                "hash": source_ref_hash,
                "now": datetime.datetime.now(datetime.UTC).isoformat(),
            },
        )

    command.upgrade(cfg, "d5e6f7a8b9c0")

    inspector = inspect(engine)
    col_nullable = {c["name"]: c["nullable"] for c in inspector.get_columns("sources")}
    assert col_nullable["source_ref"] is False
    assert col_nullable["source_ref_hash"] is False

    command.downgrade(cfg, "c1d2e3f4a5b6")

    inspector = inspect(create_engine(db_url))
    col_nullable = {c["name"]: c["nullable"] for c in inspector.get_columns("sources")}
    assert col_nullable["source_ref"] is True
    assert col_nullable["source_ref_hash"] is True
