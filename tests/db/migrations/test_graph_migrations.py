"""Behavioral tests for graph-stage table constraints in the migrated schema.

Structural equivalence to the ORM baseline (columns, nullability, indexes,
foreign keys, unique and CHECK constraints) is covered once, across every
table, by ``tests/db/migrations/test_baseline.py``. This module exercises
graph-table constraints that only a live insert can prove: composite primary
keys, foreign keys, uniqueness, and CHECK expressions enforced by SQLite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text

_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def test_graph_manifest_has_composite_primary_key(tmp_path: Path) -> None:
    """The manifest table's primary key is the composite ``(run_id, chunk_id)``.

    A scalar primary key would pass the column check yet allow a chunk to appear
    in a run only once globally rather than once per run — breaking the
    append-only manifest across re-chunks.
    """
    url = f"sqlite:///{tmp_path / 'pk.db'}"
    command.upgrade(_alembic_cfg(url), "head")

    pk = inspect(create_engine(url)).get_pk_constraint("graph_chunk_run_manifest")
    assert sorted(pk["constrained_columns"]) == ["chunk_id", "run_id"]


def test_graph_chunk_id_foreign_keys_into_chunks(tmp_path: Path) -> None:
    """Manifest and variant rows foreign-key their ``chunk_id`` into graph_chunks."""
    url = f"sqlite:///{tmp_path / 'fk.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    migrated = inspect(create_engine(url))

    for table in ("graph_chunk_run_manifest", "graph_contextualized_chunks"):
        fks = migrated.get_foreign_keys(table)
        assert any(
            fk["referred_table"] == "graph_chunks"
            and fk["referred_columns"] == ["chunk_id"]
            and fk["constrained_columns"] == ["chunk_id"]
            for fk in fks
        ), f"{table} missing chunk_id foreign key into graph_chunks"


def test_extraction_jobs_idempotency_key_unique(tmp_path: Path) -> None:
    """A duplicate ``idempotency_key`` on ``graph_extraction_jobs`` is rejected.

    Re-enqueueing the same source must reuse its open work-unit rather than
    creating a second row; the unique constraint is the database-level backing
    of that idempotency guarantee.
    """
    url = f"sqlite:///{tmp_path / 'extraction_jobs_unique.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_extraction_jobs (idempotency_key, source_id, status, attempts) "
        "VALUES (:key, :source_id, 'queued', 0)"
    )
    params = {
        "key": "source:11111111-1111-1111-1111-111111111111",
        "source_id": "11111111-1111-1111-1111-111111111111",
    }
    with engine.begin() as conn:
        conn.execute(insert, params)

    with (
        pytest.raises(Exception, match="UNIQUE|unique"),  # noqa: PT011 — driver-specific IntegrityError message
        engine.begin() as conn,
    ):
        conn.execute(insert, params)


def test_output_memo_unique_constraint_rejects_duplicate_key(tmp_path: Path) -> None:
    """The unique ``(kind, scope_id, derivation_key)`` rejects a duplicate memo row."""
    url = f"sqlite:///{tmp_path / 'memo_unique.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_contextualization_output_memo "
        "(kind, scope_id, derivation_key, output_text, created_at) "
        "VALUES (:kind, :scope, :key, :out, :now)"
    )
    params = {
        "kind": "summary",
        "scope": "11111111-1111-1111-1111-111111111111",
        "key": '{"markdown_hash":"abc"}',
        "out": "a summary",
        "now": "2026-06-06T00:00:00",
    }
    with engine.begin() as conn:
        conn.execute(insert, params)

    # A second row with the same (kind, scope_id, derivation_key) violates the unique constraint,
    # even with different output_text — the key, not the value, is unique.
    with (
        pytest.raises(Exception, match="UNIQUE|unique"),  # noqa: PT011 — driver-specific IntegrityError message
        engine.begin() as conn,
    ):
        conn.execute(insert, {**params, "out": "a different summary"})


def test_output_memo_kind_check_rejects_unknown_kind(tmp_path: Path) -> None:
    """The ``kind`` CHECK constraint rejects any value outside ``summary`` / ``revision``.

    A typo'd kind would otherwise create a durable but unreachable memo entry; the
    constraint makes that fail closed at the persistence boundary.
    """
    url = f"sqlite:///{tmp_path / 'memo_kind.db'}"
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_contextualization_output_memo "
        "(kind, scope_id, derivation_key, output_text, created_at) "
        "VALUES (:kind, :scope, :key, :out, :now)"
    )
    params = {
        # deliberately misspelled event kind — not a legal value
        "kind": "sumary",  # typos:disable
        "scope": "11111111-1111-1111-1111-111111111111",
        "key": '{"markdown_hash":"abc"}',
        "out": "an output",
        "now": "2026-06-06T00:00:00",
    }
    with (
        pytest.raises(Exception, match="CHECK|check|constraint"),  # noqa: PT011 — driver-specific message
        engine.begin() as conn,
    ):
        conn.execute(insert, params)

    # The two legal kinds insert without error.
    for kind in ("summary", "revision"):
        with engine.begin() as conn:
            conn.execute(insert, {**params, "kind": kind})
