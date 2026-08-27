"""Retype graph_chunks.source_id to the UUID storage form; rename the FTS key to scope_id.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-08-26 12:00:00.000000

``graph_chunks.source_id`` held the dashed string form of the source identity so
it compared directly against ``pipeline_runs.scope_id``. Every other
``source_id`` column in the schema is ``sa.Uuid``, which SQLite stores as
dashless hex, and the identity grammar makes the name the contract: a column
named ``source_id`` holds a UUID. This revision reconciles the one deviation.

The stored values are rewritten dashed → dashless before the declared type
changes, so the column's contents already match ``sa.Uuid``'s storage form when
the batch rebuild swaps the type in. Both directions then assert that form before
proceeding: the rewrites slice at fixed offsets, so an off-form row would be
silently reshaped into a wrong identity rather than refused.
``batch_alter_table`` is SQLite's table-recreate path; it reflects and restores
the two indexes.

The index ``graph_content_fts`` keeps holding the **dashed** identity, because it
exists to join ``pipeline_runs.scope_id``; its column is renamed ``source_id`` →
``scope_id`` to say so, in the same revision that makes ``source_id`` mean the
UUID form everywhere else. An FTS5 column cannot be renamed in place, so the
table is dropped, recreated, and rebuilt from every committed ``graph_chunks`` /
``graph_contextualized_chunks`` row — the index is derived state, so a rebuild
loses nothing. The rebuild renders the dashed form from the newly-dashless column;
that rendering is duplicated from ``aizk.graph.content_index`` because migrations
stay self-contained and do not import app code.

Every prior revision's inline FTS backfill stays as written: each targets the
schema of its own revision, where the column was still ``source_id`` holding the
dashed string.

This lives in the conversion Alembic tree, which owns all tables for the single
SQLite database (ADR-003).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Exactly 32 lowercase hex characters — the form ``sa.Uuid`` stores under SQLite.
#: Bound as a parameter, never interpolated.
_UUID_STORAGE_GLOB = "[0-9a-f]" * 32

#: Rebuild the dashed form from dashless hex, at the 8-4-4-4-12 group boundaries.
#: Correct only for values matching :data:`_UUID_STORAGE_GLOB`, checked first.
_REDASH_SQL = (
    "UPDATE graph_chunks SET source_id = "
    "substr(source_id, 1, 8) || '-' || substr(source_id, 9, 4) || '-' || "
    "substr(source_id, 13, 4) || '-' || substr(source_id, 17, 4) || '-' || substr(source_id, 21, 12)"
)

#: The same rendering as an expression over the aliased ``graph_chunks`` row, for
#: the FTS rebuild that reads the column while it is still dashless.
_SCOPE_KEY_SQL = (
    "substr(c.source_id, 1, 8) || '-' || substr(c.source_id, 9, 4) || '-' || "
    "substr(c.source_id, 13, 4) || '-' || substr(c.source_id, 17, 4) || '-' || substr(c.source_id, 21, 12)"
)

#: The index after this revision: the identity column is named for what it joins.
_FTS_DDL_SCOPE_ID = (
    "CREATE VIRTUAL TABLE graph_content_fts USING fts5("
    "text, kind UNINDEXED, chunk_id UNINDEXED, run_id UNINDEXED, scope_id UNINDEXED)"
)

#: The index as the prior revision left it, restored on downgrade.
_FTS_DDL_SOURCE_ID = (
    "CREATE VIRTUAL TABLE graph_content_fts USING fts5("
    "text, kind UNINDEXED, chunk_id UNINDEXED, run_id UNINDEXED, source_id UNINDEXED)"
)

#: Repopulate the renamed index, rendering the dashed key from the retyped column.
_BACKFILL_SCOPE_ID = (
    (
        "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "  # noqa: S608 — fragment is a constant
        f"SELECT c.text, 'chunk', c.chunk_id, NULL, {_SCOPE_KEY_SQL} FROM graph_chunks c"
    ),
    (
        "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "  # noqa: S608 — fragment is a constant
        "SELECT CASE WHEN cc.contextualized_text = '' THEN c.text ELSE cc.contextualized_text END, "
        f"'contextualized', cc.chunk_id, cc.run_id, {_SCOPE_KEY_SQL} "
        "FROM graph_contextualized_chunks cc JOIN graph_chunks c ON c.chunk_id = cc.chunk_id"
    ),
)

#: Repopulate the restored index. The column is dashed again by this point, so the
#: value copies across verbatim, as it did before this revision.
_BACKFILL_SOURCE_ID = (
    (
        "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, source_id) "
        "SELECT c.text, 'chunk', c.chunk_id, NULL, c.source_id FROM graph_chunks c"
    ),
    (
        "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, source_id) "
        "SELECT CASE WHEN cc.contextualized_text = '' THEN c.text ELSE cc.contextualized_text END, "
        "'contextualized', cc.chunk_id, cc.run_id, c.source_id "
        "FROM graph_contextualized_chunks cc JOIN graph_chunks c ON c.chunk_id = cc.chunk_id"
    ),
)


def _recreate_content_index(ddl: str, backfill: tuple[str, ...]) -> None:
    """Drop the FTS index, recreate it with ``ddl``, and repopulate it via ``backfill``.

    An FTS5 column cannot be renamed in place. The index is derived state, so
    dropping and rebuilding it from the committed source rows is lossless.

    Args:
        ddl: The ``CREATE VIRTUAL TABLE`` statement for the desired shape.
        backfill: The insert-select statements repopulating it, in order.
    """
    op.execute("DROP TABLE IF EXISTS graph_content_fts")
    op.execute(ddl)
    for statement in backfill:
        op.execute(statement)


def _assert_uuid_storage_form() -> None:
    """Fail if any ``graph_chunks.source_id`` is not 32 lowercase hex characters.

    Both directions slice at fixed offsets, so a value of the wrong length or case
    would be rewritten into a plausible-looking but wrong identity rather than
    rejected. Running this between the rewrite and the type change means a bad row
    stops the migration while the data is still in its original shape.

    Raises:
        ValueError: If any row is outside the storage form.
    """
    offenders = (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM graph_chunks WHERE source_id NOT GLOB :storage_form"),
            {"storage_form": _UUID_STORAGE_GLOB},
        )
        .scalar_one()
    )
    if offenders:
        raise ValueError(
            f"{offenders} graph_chunks row(s) hold a source_id outside the 32 lowercase hex storage form; "
            "resolve them before migrating the column"
        )


def upgrade() -> None:
    """Rewrite the values to dashless hex, retype the column, and rebuild the index.

    Raises:
        ValueError: If the rewrite did not yield the ``sa.Uuid`` storage form for
            every row, which means an input was not a canonical dashed UUID.
    """
    op.execute("UPDATE graph_chunks SET source_id = replace(source_id, '-', '')")
    _assert_uuid_storage_form()
    with op.batch_alter_table("graph_chunks") as batch:
        batch.alter_column(
            "source_id",
            existing_type=sa.String(),
            type_=sa.Uuid(),
            existing_nullable=False,
        )
    _recreate_content_index(_FTS_DDL_SCOPE_ID, _BACKFILL_SCOPE_ID)


def downgrade() -> None:
    """Restore the string column and the dashed values, then rebuild the prior index.

    The index is rebuilt last, so it reads ``graph_chunks.source_id`` after the
    re-dashing and copies the value across without rendering.

    Raises:
        ValueError: If any stored value is outside the ``sa.Uuid`` storage form the
            re-dashing expects.
    """
    _assert_uuid_storage_form()
    with op.batch_alter_table("graph_chunks") as batch:
        batch.alter_column(
            "source_id",
            existing_type=sa.Uuid(),
            type_=sa.String(),
            existing_nullable=False,
        )
    op.execute(_REDASH_SQL)
    _recreate_content_index(_FTS_DDL_SOURCE_ID, _BACKFILL_SOURCE_ID)
