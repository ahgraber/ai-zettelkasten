"""Schema-fidelity tests for the graph-stage chunk tables migration.

Asserts the migrated schema for the four graph tables is structurally
equivalent to the ORM baseline (``create_all``) — columns and nullability,
indexes, foreign keys, and unique constraints — and that the revision downgrades
cleanly without disturbing the conversion or pipeline tables. Scoped to the
graph tables so it is independent of any models other test modules register on
``SQLModel.metadata``; the full cross-table parity check lives in the conversion
migration suite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel

from aizk.graph.datamodel import (
    Chunk,
    ChunkRunInput,
    ChunkRunManifest,
    ContextualizationJob,
    ContextualizationOutputMemo,
    ContextualizedChunk,
    DocumentSummary,
)

_MIGRATIONS_DIR = Path(importlib.util.find_spec("aizk.db.migrations").origin).resolve().parent

# The graph tables span three migrations on top of the pipeline-runtime revision:
# f8b9c0d1e2a3 (chunk/contextualization tables), a9c0d1e2f3b4 (work-unit table),
# and c2d3e4f5a6b7 (output memo). Downgrading to _PREV_REVISION removes all of them.
_PREV_REVISION = "e1f2a3b4c5d6"
_GRAPH_TABLES = (
    "graph_chunks",
    "graph_chunk_run_inputs",
    "graph_chunk_run_manifest",
    "graph_document_summaries",
    "graph_contextualized_chunks",
    "graph_contextualization_jobs",
    "graph_contextualization_output_memo",
)
_GRAPH_ORM_TABLES = [
    Chunk.__table__,
    ChunkRunInput.__table__,
    ChunkRunManifest.__table__,
    DocumentSummary.__table__,
    ContextualizedChunk.__table__,
    ContextualizationJob.__table__,
    ContextualizationOutputMemo.__table__,
]


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


def test_graph_tables_match_create_all(tmp_path: Path) -> None:
    """Running migrations produces the same graph-table schema as create_all."""
    migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    baseline_url = f"sqlite:///{tmp_path / 'baseline.db'}"

    command.upgrade(_alembic_cfg(migrated_url), "head")

    baseline_engine = create_engine(baseline_url)
    SQLModel.metadata.create_all(baseline_engine, tables=_GRAPH_ORM_TABLES)

    migrated = inspect(create_engine(migrated_url))
    baseline = inspect(baseline_engine)

    migrated_tables = set(migrated.get_table_names())
    assert set(_GRAPH_TABLES) <= migrated_tables, f"missing graph tables: {migrated_tables}"

    for table in _GRAPH_TABLES:
        baseline_cols = {c["name"]: c["nullable"] for c in baseline.get_columns(table)}
        migrated_cols = {c["name"]: c["nullable"] for c in migrated.get_columns(table)}
        assert migrated_cols == baseline_cols, f"{table} column/nullable mismatch"

        assert {_normalize_index(i) for i in migrated.get_indexes(table)} == {
            _normalize_index(i) for i in baseline.get_indexes(table)
        }, f"{table} index mismatch"

        assert {_normalize_fk(fk) for fk in migrated.get_foreign_keys(table)} == {
            _normalize_fk(fk) for fk in baseline.get_foreign_keys(table)
        }, f"{table} foreign key mismatch"

        baseline_pk = sorted(baseline.get_pk_constraint(table)["constrained_columns"])
        migrated_pk = sorted(migrated.get_pk_constraint(table)["constrained_columns"])
        assert migrated_pk == baseline_pk, f"{table} primary key mismatch"

        baseline_uniques = {tuple(sorted(uc["column_names"])) for uc in baseline.get_unique_constraints(table)}
        migrated_uniques = {tuple(sorted(uc["column_names"])) for uc in migrated.get_unique_constraints(table)}
        assert migrated_uniques == baseline_uniques, f"{table} unique constraint mismatch"


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


_MEMO_REVISION = "c2d3e4f5a6b7"
_MEMO_PREV_REVISION = "b0d1e2f3a4c5"


def test_output_memo_migration_round_trips(tmp_path: Path) -> None:
    """The output-memo revision creates and drops its table cleanly on a scratch DB."""
    url = f"sqlite:///{tmp_path / 'memo_round_trip.db'}"
    cfg = _alembic_cfg(url)

    command.upgrade(cfg, _MEMO_REVISION)
    assert "graph_contextualization_output_memo" in inspect(create_engine(url)).get_table_names()

    command.downgrade(cfg, _MEMO_PREV_REVISION)
    tables_after = set(inspect(create_engine(url)).get_table_names())
    assert "graph_contextualization_output_memo" not in tables_after
    # The prior graph tables are untouched by the memo downgrade.
    assert "graph_contextualized_chunks" in tables_after
    assert "graph_contextualization_jobs" in tables_after


def test_output_memo_unique_constraint_rejects_duplicate_key(tmp_path: Path) -> None:
    """The unique ``(kind, scope_key, derivation_key)`` rejects a duplicate memo row."""
    url = f"sqlite:///{tmp_path / 'memo_unique.db'}"
    command.upgrade(_alembic_cfg(url), _MEMO_REVISION)
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_contextualization_output_memo "
        "(kind, scope_key, derivation_key, output_text, created_at) "
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

    # A second row with the same (kind, scope_key, derivation_key) violates the unique constraint,
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
    command.upgrade(_alembic_cfg(url), _MEMO_REVISION)
    engine = create_engine(url)

    insert = text(
        "INSERT INTO graph_contextualization_output_memo "
        "(kind, scope_key, derivation_key, output_text, created_at) "
        "VALUES (:kind, :scope, :key, :out, :now)"
    )
    params = {
        "kind": "sumary",  # deliberate typo — not a legal kind
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


def test_graph_revision_downgrade_drops_only_graph_tables(tmp_path: Path) -> None:
    """Downgrading one revision drops the graph tables and leaves the rest intact."""
    url = f"sqlite:///{tmp_path / 'down.db'}"
    cfg = _alembic_cfg(url)

    command.upgrade(cfg, "head")
    tables_at_head = set(inspect(create_engine(url)).get_table_names())
    assert set(_GRAPH_TABLES) <= tables_at_head

    command.downgrade(cfg, _PREV_REVISION)
    tables_after = set(inspect(create_engine(url)).get_table_names())

    assert not (set(_GRAPH_TABLES) & tables_after), "graph tables should be dropped"
    assert "conversion_jobs" in tables_after, "conversion tables remain after downgrade"
    assert "pipeline_runs" in tables_after, "pipeline tables remain after downgrade"
    assert "pipeline_events" in tables_after
