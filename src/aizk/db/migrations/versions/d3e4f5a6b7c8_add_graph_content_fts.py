"""Add the graph content full-text search index.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-06-14 12:00:00.000000

Creates ``graph_content_fts``, a single SQLite FTS5 virtual table indexing both
the raw chunk text (``kind='chunk'``) and each contextualized variant
(``kind='contextualized'``) so the operator UI can full-text search content. Only
``text`` is indexed; ``kind`` / ``chunk_id`` / ``run_id`` / ``doc_id`` are
``UNINDEXED`` so the search query can filter and label matches without a side
join.

The migration first verifies FTS5 is compiled into the SQLite build and fails
clearly if not (FTS5 is a deployment prerequisite, including for Litestream's
SQLite), then creates the table and **backfills it from every committed**
``graph_chunks`` and ``graph_contextualized_chunks`` row — not only the active
generation — so currency is decided at query time and a ``chunk_id`` superseded
now but reused in a later active run stays searchable. An empty contextualized
revision indexes the raw chunk text (a self-contained chunk's contextualized
representation is its raw text). Inserts come only from committed records; the
contextualization output memo is never indexed.

The index is rebuildable derived state: this backfill SQL is duplicated in
``aizk.graph.content_index.rebuild_content_index`` (migrations stay self-contained
and do not import app code, so the duplication is intentional and guarded by a
rebuild-reproduces-content test).

This lives in the conversion Alembic tree, which owns all tables for the single
SQLite database (ADR-003).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTENT_FTS_DDL = (
    "CREATE VIRTUAL TABLE graph_content_fts USING fts5("
    "text, kind UNINDEXED, chunk_id UNINDEXED, run_id UNINDEXED, doc_id UNINDEXED)"
)

_BACKFILL_CHUNKS_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, doc_id) "
    "SELECT text, 'chunk', chunk_id, NULL, doc_id FROM graph_chunks"
)

_BACKFILL_CONTEXTUALIZED_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, doc_id) "
    "SELECT CASE WHEN cc.contextualized_text = '' THEN c.text ELSE cc.contextualized_text END, "
    "'contextualized', cc.chunk_id, cc.run_id, c.doc_id "
    "FROM graph_contextualized_chunks cc JOIN graph_chunks c ON c.chunk_id = cc.chunk_id"
)


def _assert_fts5_available(bind: sa.engine.Connectable) -> None:
    """Raise a clear error if FTS5 is not compiled into the bound SQLite build.

    Probes by creating and dropping a temporary FTS5 table on the connection. Any
    failure (FTS5 absent in this SQLite build) is re-raised as a ``RuntimeError``
    naming FTS5 as a deployment prerequisite, so the migration fails loudly rather
    than leaving a silently broken search surface.

    Args:
        bind: The connection (or engine) the migration runs against.

    Raises:
        RuntimeError: If a probe FTS5 table cannot be created.
    """
    try:
        bind.execute(sa.text("CREATE VIRTUAL TABLE temp.aizk_fts5_probe USING fts5(x)"))
        bind.execute(sa.text("DROP TABLE temp.aizk_fts5_probe"))
    except Exception as exc:  # noqa: BLE001 — surface any FTS5-unavailable failure uniformly
        raise RuntimeError(
            "SQLite FTS5 is required for the graph content search index but is not "
            "compiled into this SQLite build. FTS5 is a deployment prerequisite "
            "(including for Litestream's SQLite). Original error: " + str(exc)
        ) from exc


def upgrade() -> None:
    """Verify FTS5, create the content index, and backfill all committed rows."""
    bind = op.get_bind()
    _assert_fts5_available(bind)

    op.execute(_CONTENT_FTS_DDL)
    op.execute(_BACKFILL_CHUNKS_SQL)
    op.execute(_BACKFILL_CONTEXTUALIZED_SQL)


def downgrade() -> None:
    """Drop the content index; SQLite drops the FTS5 shadow tables automatically."""
    op.execute("DROP TABLE graph_content_fts")
