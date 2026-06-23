"""Surrogate chunk_id and rename the source identity to source_id (scope_key to scope_id).

Revision ID: f1a2b3c4d5e6
Revises: d3e4f5a6b7c8
Create Date: 2026-06-22 12:00:00.000000

Turns ``graph_chunks.chunk_id`` into a stable surrogate identity and renames the
durable source identity to ``source_id``, in one revision covering three concerns:

1. **``chunk_id`` surrogate.**
   ``chunk_id`` was a content-addressed ``xxh64(source_id, heading_path, ordinal,
   content_hash)`` digest, so every existing row already has a distinct
   sameness-key; a fresh UUID surrogate is assigned per row and every reference is
   repointed through the old→new map (``graph_chunk_run_manifest`` and
   ``graph_contextualized_chunks`` ``chunk_id``, plus ``graph_content_fts`` via its
   end-of-upgrade rebuild). A unique ``ix_graph_chunks_sameness_key`` on
   ``(source_id, heading_path_json, ordinal, content_hash)`` backs run-independent
   identity reuse. The PK column ``chunk_id`` is unchanged structurally — only its
   values become surrogates. (Greenfield: dev-only data, but written to be correct
   for populated databases.)
2. **``aizk_uuid`` → ``source_id``** across the source table and dependents.
3. **``scope_key`` → ``scope_id``** on the run primitive's tables.

The surrogate step runs after the column renames so it references the final
``source_id`` column name directly; the revision's end-state is independent of
that internal ordering.

Renames the canonical source-identity column from ``aizk_uuid`` to ``source_id``
across the ``sources`` table and every dependent table (``conversion_jobs`` and
``conversion_outputs`` foreign keys, ``pipeline_events``, ``graph_chunks`` —
formerly ``doc_id`` — and ``graph_contextualization_jobs``), and the run
primitive's scope reference from ``scope_key`` to ``scope_id`` (``pipeline_runs``
and ``graph_contextualization_output_memo``). Single canonical role-name for the
source identity, and the ``_id`` suffix for the scope identity, per the
pipeline-identity grammar.

Column renames use SQLite's native ``ALTER TABLE ... RENAME COLUMN``, which
preserves data and propagates the rename into foreign-key definitions of
referencing tables, so the ``conversion_jobs`` / ``conversion_outputs`` →
``sources`` edges stay intact. Indexes whose *name* embeds the old token are
dropped and recreated under the new name; indexes whose name does not (the
partial unique ``uq_pipeline_runs_active_scope`` and the output-memo indexes)
keep their name and have their column reference updated by the column rename.

``graph_content_fts`` is an FTS5 virtual table whose ``doc_id`` column cannot be
renamed in place, so it is dropped and recreated with the ``source_id`` column,
then rebuilt from every committed ``graph_chunks`` / ``graph_contextualized_chunks``
row — the same all-committed-rows backfill the index's creating migration
performs (duplicated here because migrations stay self-contained and do not import
app code; the rebuild-reproduces-content test guards the two against drift).

This lives in the conversion Alembic tree, which owns all tables for the single
SQLite database (ADR-003).
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from uuid import uuid4

from alembic import op
from sqlalchemy import text
import xxhash

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``(table, old_column, new_column)``. Ordered so the referenced ``sources``
#: column is renamed before the tables whose foreign keys reference it.
_COLUMN_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("sources", "aizk_uuid", "source_id"),
    ("conversion_jobs", "aizk_uuid", "source_id"),
    ("conversion_outputs", "aizk_uuid", "source_id"),
    ("pipeline_events", "aizk_uuid", "source_id"),
    ("pipeline_runs", "scope_key", "scope_id"),
    ("graph_chunks", "doc_id", "source_id"),
    ("graph_contextualization_jobs", "aizk_uuid", "source_id"),
    ("graph_contextualization_output_memo", "scope_key", "scope_id"),
)

#: ``(old_name, old_columns, new_name, new_columns, table, unique)`` for indexes
#: whose name embeds the renamed token and must be recreated under the new name.
_INDEX_RENAMES: tuple[tuple[str, str, str, str, str, bool], ...] = (
    ("ix_sources_aizk_uuid", "aizk_uuid", "ix_sources_source_id", "source_id", "sources", True),
    (
        "ix_conversion_jobs_aizk_uuid",
        "aizk_uuid",
        "ix_conversion_jobs_source_id",
        "source_id",
        "conversion_jobs",
        False,
    ),
    (
        "ix_conversion_outputs_aizk_uuid",
        "aizk_uuid",
        "ix_conversion_outputs_source_id",
        "source_id",
        "conversion_outputs",
        False,
    ),
    (
        "ix_pipeline_events_aizk_uuid_occurred_at",
        "aizk_uuid, occurred_at",
        "ix_pipeline_events_source_id_occurred_at",
        "source_id, occurred_at",
        "pipeline_events",
        False,
    ),
    (
        "ix_pipeline_runs_stage_scope_key",
        "stage, scope_key",
        "ix_pipeline_runs_stage_scope_id",
        "stage, scope_id",
        "pipeline_runs",
        False,
    ),
    ("ix_graph_chunks_doc_id", "doc_id", "ix_graph_chunks_source_id", "source_id", "graph_chunks", False),
    (
        "ix_graph_contextualization_jobs_aizk_uuid",
        "aizk_uuid",
        "ix_graph_contextualization_jobs_source_id",
        "source_id",
        "graph_contextualization_jobs",
        False,
    ),
)

# FTS5 virtual table recreated with the renamed ``source_id`` column (upgrade) or
# the original ``doc_id`` column (downgrade). The backfill mirrors the index's
# creating migration: every committed chunk row, plus every committed variant
# (an empty revision indexes the raw chunk text).
_FTS_DDL = (
    "CREATE VIRTUAL TABLE graph_content_fts USING fts5("
    "text, kind UNINDEXED, chunk_id UNINDEXED, run_id UNINDEXED, {col} UNINDEXED)"
)
_BACKFILL_CHUNKS = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, {col}) "
    "SELECT text, 'chunk', chunk_id, NULL, {col} FROM graph_chunks"
)
_BACKFILL_CONTEXTUALIZED = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, {col}) "
    "SELECT CASE WHEN cc.contextualized_text = '' THEN c.text ELSE cc.contextualized_text END, "
    "'contextualized', cc.chunk_id, cc.run_id, c.{col} "
    "FROM graph_contextualized_chunks cc JOIN graph_chunks c ON c.chunk_id = cc.chunk_id"
)


def _recreate_content_fts(col: str) -> None:
    """Drop and rebuild ``graph_content_fts`` with ``col`` as the source-identity column."""
    op.execute("DROP TABLE graph_content_fts")
    op.execute(_FTS_DDL.format(col=col))
    op.execute(_BACKFILL_CHUNKS.format(col=col))
    op.execute(_BACKFILL_CONTEXTUALIZED.format(col=col))


#: Tables whose ``chunk_id`` is repointed through the surrogate map (graph_chunks
#: first as the identity, then its referrers). ``graph_content_fts`` is excluded:
#: it is rebuilt from these tables at the end of the migration and picks up the
#: surrogate values automatically. FK enforcement is off during migrations
#: (SQLite default), so repointing the PK and its referrers needs no special order.
_CHUNK_ID_TABLES: tuple[str, ...] = (
    "graph_chunks",
    "graph_chunk_run_manifest",
    "graph_contextualized_chunks",
)

_SAMENESS_INDEX_DDL = (
    "CREATE UNIQUE INDEX ix_graph_chunks_sameness_key "
    "ON graph_chunks (source_id, heading_path_json, ordinal, content_hash)"
)


def _repoint_chunk_ids(mapping: dict[str, str]) -> None:
    """Rewrite every ``chunk_id`` reference (identity row + referrers) through ``mapping``."""
    conn = op.get_bind()
    for old, new in mapping.items():
        for table in _CHUNK_ID_TABLES:
            conn.execute(
                text(f"UPDATE {table} SET chunk_id = :new WHERE chunk_id = :old"),  # noqa: S608 — table from a fixed allowlist
                {"new": new, "old": old},
            )


def _content_addressed_chunk_id(source_id: str, heading_path_json: str, ordinal: int, content_hash: str) -> str:
    """Recompute the pre-surrogate content-addressed ``chunk_id`` for the downgrade.

    Mirrors ``aizk.chunking.datamodel.derive_chunk_content_key`` (migrations stay
    self-contained and do not import app code); the round-trip-downgrade test
    guards the two against drift.
    """
    canonical = json.dumps(
        [source_id, json.loads(heading_path_json), ordinal, content_hash],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return xxhash.xxh64(canonical.encode("utf-8")).hexdigest()


def _assign_chunk_surrogates() -> None:
    """Assign a UUID surrogate per existing chunk row, repoint references, add the sameness index."""
    conn = op.get_bind()
    old_ids = [row[0] for row in conn.execute(text("SELECT chunk_id FROM graph_chunks")).fetchall()]
    _repoint_chunk_ids({old: str(uuid4()) for old in old_ids})
    op.execute(_SAMENESS_INDEX_DDL)


def _restore_content_addressed_chunk_ids() -> None:
    """Reverse the surrogate step: drop the sameness index and repoint to content-addressed ids."""
    op.execute("DROP INDEX ix_graph_chunks_sameness_key")
    conn = op.get_bind()
    rows = conn.execute(
        text("SELECT chunk_id, source_id, heading_path_json, ordinal, content_hash FROM graph_chunks")
    ).fetchall()
    _repoint_chunk_ids(
        {
            surrogate: _content_addressed_chunk_id(source_id, heading_path_json, ordinal, content_hash)
            for surrogate, source_id, heading_path_json, ordinal, content_hash in rows
        }
    )


def upgrade() -> None:
    """Surrogate ``chunk_id``, rename the source identity to ``source_id`` and the scope to ``scope_id``."""
    for table, old, new in _COLUMN_RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
    for old_name, _old_cols, new_name, new_cols, table, unique in _INDEX_RENAMES:
        op.execute(f"DROP INDEX {old_name}")
        op.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX {new_name} ON {table} ({new_cols})")
    # After the renames (so it references ``source_id``); before the FTS rebuild, so
    # the rebuild backfills the surrogate ``chunk_id`` values.
    _assign_chunk_surrogates()
    _recreate_content_fts("source_id")


def downgrade() -> None:
    """Reverse the surrogate step and the rename, restoring content-addressed ``chunk_id`` and prior names."""
    # Reverse the surrogate step first, while the columns are still named ``source_id``
    # (the content-addressed id is computed from them).
    _restore_content_addressed_chunk_ids()
    for table, old, new in reversed(_COLUMN_RENAMES):
        op.execute(f"ALTER TABLE {table} RENAME COLUMN {new} TO {old}")
    for old_name, old_cols, new_name, _new_cols, table, unique in _INDEX_RENAMES:
        op.execute(f"DROP INDEX {new_name}")
        op.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX {old_name} ON {table} ({old_cols})")
    _recreate_content_fts("doc_id")
