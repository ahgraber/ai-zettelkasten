"""Retype pipeline_runs.scope_id and its dependents to the UUID storage form.

Revision ID: d2e3f4a5b6c7
Revises: c4d5e6f7a8b9
Create Date: 2026-09-02 12:00:00.000000

``pipeline_runs.scope_id`` held the dashed string form of a source identity, so
comparing it against a UUID-typed identity column (``sources.source_id``,
``graph_chunks.source_id``, a work-unit table's ``source_id``) required converting
one side in Python first. Every real scope is a source UUID, so the string
flexibility bought nothing while its cost recurred. This revision retypes
``scope_id`` to the ORM ``Uuid`` type everywhere it is persisted:
``pipeline_runs.scope_id``, ``graph_contextualization_output_memo.scope_id`` (the
contextualization output memo's key), and ``graph_content_fts.scope_id`` (the
content index's join key, joined against ``pipeline_runs.scope_id``).

The stored values are rewritten dashed → dashless before the declared type
changes on the two real columns, so their contents already match ``sa.Uuid``'s
storage form when the batch rebuild swaps the type in; ``graph_content_fts`` is an
FTS5 virtual table with no type affinity, so only the data is rewritten — no DDL
change, no column rename. Both directions assert the 32-lowercase-hex storage form
before proceeding: the rewrites slice at fixed offsets, so an off-form row would be
silently reshaped into a wrong identity rather than refused.

``batch_alter_table`` is SQLite's table-recreate path; it reflects and restores
every index and constraint on the two real tables, including the partial unique
index ``uq_pipeline_runs_active_scope`` (with its ``sqlite_where``),
``ix_pipeline_runs_stage_scope_id``, and the memo's unique constraint and index.

This lives in the conversion Alembic tree, which owns all tables for the single
SQLite database (ADR-003).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Exactly 32 lowercase hex characters — the form ``sa.Uuid`` stores under SQLite.
#: Bound as a parameter, never interpolated.
_UUID_STORAGE_GLOB = "[0-9a-f]" * 32

#: Every table carrying a ``scope_id`` column this revision retypes or rewrites.
#: Fixed allowlist, never user input, so it is safe to interpolate into SQL.
_SCOPE_TABLES: tuple[str, ...] = ("pipeline_runs", "graph_contextualization_output_memo", "graph_content_fts")


def _redash_sql(table: str) -> str:
    """Return the fixed-offset ``UPDATE`` restoring ``table.scope_id``'s dashed form.

    Correct only for values matching :data:`_UUID_STORAGE_GLOB`, checked first.
    """
    return (
        f"UPDATE {table} SET scope_id = "  # noqa: S608 — table from a fixed allowlist
        "substr(scope_id, 1, 8) || '-' || substr(scope_id, 9, 4) || '-' || "
        "substr(scope_id, 13, 4) || '-' || substr(scope_id, 17, 4) || '-' || substr(scope_id, 21, 12)"
    )


def _assert_uuid_storage_form() -> None:
    """Fail if any of the three tables' ``scope_id`` is not 32 lowercase hex characters.

    Both directions slice at fixed offsets, so a value of the wrong length or case
    would be rewritten into a plausible-looking but wrong identity rather than
    rejected. Running this between the rewrite and the type change means a bad row
    stops the migration while the data is still in its original shape.

    Raises:
        ValueError: If any row, in any of the three tables, is outside the storage
            form.
    """
    conn = op.get_bind()
    offenders: dict[str, int] = {}
    for table in _SCOPE_TABLES:
        count = conn.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE scope_id NOT GLOB :storage_form"),  # noqa: S608
            {"storage_form": _UUID_STORAGE_GLOB},
        ).scalar_one()
        if count:
            offenders[table] = count
    if offenders:
        detail = ", ".join(f"{table}={count}" for table, count in offenders.items())
        raise ValueError(
            f"scope_id row(s) outside the 32 lowercase hex storage form ({detail}); "
            "resolve them before migrating the column"
        )


def upgrade() -> None:
    """Rewrite the values to dashless hex, retype the two real columns, and leave the FTS index in place.

    Raises:
        ValueError: If the rewrite did not yield the ``sa.Uuid`` storage form for
            every row in every affected table, which means an input was not a
            canonical dashed UUID.
    """
    for table in _SCOPE_TABLES:
        op.execute(f"UPDATE {table} SET scope_id = replace(scope_id, '-', '')")  # noqa: S608 — fixed allowlist
    _assert_uuid_storage_form()
    with op.batch_alter_table("pipeline_runs") as batch:
        batch.alter_column(
            "scope_id",
            existing_type=sa.String(),
            type_=sa.Uuid(),
            existing_nullable=False,
        )
    with op.batch_alter_table("graph_contextualization_output_memo") as batch:
        batch.alter_column(
            "scope_id",
            existing_type=sa.String(),
            type_=sa.Uuid(),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Restore the string columns and the dashed values everywhere, including the FTS index.

    Raises:
        ValueError: If any stored value is outside the ``sa.Uuid`` storage form the
            re-dashing expects.
    """
    _assert_uuid_storage_form()
    with op.batch_alter_table("graph_contextualization_output_memo") as batch:
        batch.alter_column(
            "scope_id",
            existing_type=sa.Uuid(),
            type_=sa.String(),
            existing_nullable=False,
        )
    with op.batch_alter_table("pipeline_runs") as batch:
        batch.alter_column(
            "scope_id",
            existing_type=sa.Uuid(),
            type_=sa.String(),
            existing_nullable=False,
        )
    for table in _SCOPE_TABLES:
        op.execute(_redash_sql(table))
