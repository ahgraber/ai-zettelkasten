"""Retype the chunk and mention surrogate identities to the UUID storage form.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-02 12:00:00.000000

``graph_chunks.chunk_id``, ``graph_chunk_run_manifest.chunk_id``,
``graph_contextualized_chunks.chunk_id``, ``graph_mentions.mention_id``,
``graph_mentions.chunk_id``, and ``graph_mention_cooccurrences.{chunk_id,
mention_id_lo,mention_id_hi}`` held the dashed string form of their surrogate
identity. Every other surrogate identity in the schema (``source_id``,
``scope_id``) is already ``sa.Uuid``; this revision reconciles the remaining
deviation so the identity-naming convention holds without exception: a column
named ``chunk_id`` or ``mention_id`` holds a UUID.

The stored values are rewritten dashed -> dashless before the declared types
change, so the columns' contents already match ``sa.Uuid``'s storage form when
the batch rebuild swaps the types in. Both directions assert the 32-lowercase-hex
storage form before proceeding: the rewrites slice at fixed offsets, so an
off-form row would be silently reshaped into a wrong identity rather than
refused.

``graph_content_fts`` is an FTS5 virtual table with no type affinity, so only its
``chunk_id`` data is rewritten — no DDL change, no column rename (the same
treatment ``d2e3f4a5b6c7`` gave ``graph_content_fts.scope_id``). The search
join (``aizk.graph.search``'s ``_SEARCH_SQL``, ``m.chunk_id = f.chunk_id``)
compiles to a raw column-to-column comparison that does not pass through either
side's bind processor, so every column on both sides of every join between them
must already hold the dashless form when the type change lands — all eight real
columns plus the FTS rewrite happen atomically in one revision, exactly as
``c4d5e6f7a8b9`` reasoned for ``source_id``.

``batch_alter_table`` is SQLite's table-recreate path; it reflects and restores
each table's indexes and constraints, including ``graph_chunk_run_manifest``'s
composite primary key ``(run_id, chunk_id)``, ``graph_mention_cooccurrences``'s
composite primary key ``(run_id, mention_id_lo, mention_id_hi)`` and its
``CHECK (mention_id_lo < mention_id_hi)``, and ``graph_mentions``'s two partial
unique indexes and four ``CHECK`` constraints. Dashes sit at identical fixed
positions in every canonical UUID, so ``replace(col, '-', '')`` cannot change the
relative lexicographic order of two values — the ordering the co-occurrence
``CHECK`` depends on survives the rewrite.

Ordering among the five tables does not matter: ``src/aizk/db/migrations/env.py``
builds a bare ``create_engine`` and does not set ``PRAGMA foreign_keys=ON``,
unlike ``src/aizk/graph/db.py``, so no foreign key enforces an ordering
constraint mid-migration.

This lives in the conversion Alembic tree, which owns all tables for the single
SQLite database (ADR-003).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Exactly 32 lowercase hex characters — the form ``sa.Uuid`` stores under SQLite.
#: Bound as a parameter, never interpolated.
_UUID_STORAGE_GLOB = "[0-9a-f]" * 32

#: Every ``(table, column)`` this revision retypes or rewrites — the eight real
#: ORM columns plus the FTS index's ``chunk_id``. Fixed allowlist, never user
#: input, so it is safe to interpolate into SQL.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("graph_chunks", "chunk_id"),
    ("graph_chunk_run_manifest", "chunk_id"),
    ("graph_contextualized_chunks", "chunk_id"),
    ("graph_mentions", "mention_id"),
    ("graph_mentions", "chunk_id"),
    ("graph_mention_cooccurrences", "chunk_id"),
    ("graph_mention_cooccurrences", "mention_id_lo"),
    ("graph_mention_cooccurrences", "mention_id_hi"),
    ("graph_content_fts", "chunk_id"),
)

#: :data:`_COLUMNS` grouped by table, in column order, for the rewrite passes.
#: ``graph_mention_cooccurrences`` carries ``CHECK (mention_id_lo < mention_id_hi)``,
#: which SQLite re-evaluates on every row-level ``UPDATE``: rewriting
#: ``mention_id_lo`` and ``mention_id_hi`` in two separate statements would
#: transiently compare one dashed and one dashless value, and ``'-'`` (0x2D)
#: sorts below every hex digit, so the CHECK can fail mid-migration depending on
#: which column is rewritten first. Rewriting every column of a table in one
#: ``UPDATE`` keeps each row's CHECK-relevant columns mutually consistent at
#: every commit point.
_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "graph_chunks": ("chunk_id",),
    "graph_chunk_run_manifest": ("chunk_id",),
    "graph_contextualized_chunks": ("chunk_id",),
    "graph_mentions": ("mention_id", "chunk_id"),
    "graph_mention_cooccurrences": ("chunk_id", "mention_id_lo", "mention_id_hi"),
    "graph_content_fts": ("chunk_id",),
}


def _assert_uuid_storage_form() -> None:
    """Fail if any of the nine ``(table, column)`` pairs is not 32 lowercase hex characters.

    Both directions slice at fixed offsets, so a value of the wrong length or case
    would be rewritten into a plausible-looking but wrong identity rather than
    rejected. Running this between the rewrite and the type change means a bad row
    stops the migration while the data is still in its original shape.

    Raises:
        ValueError: If any row, in any of the nine columns, is outside the
            storage form.
    """
    conn = op.get_bind()
    offenders: dict[str, int] = {}
    for table, column in _COLUMNS:
        count = conn.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE {column} NOT GLOB :storage_form"),  # noqa: S608
            {"storage_form": _UUID_STORAGE_GLOB},
        ).scalar_one()
        if count:
            offenders[f"{table}.{column}"] = count
    if offenders:
        detail = ", ".join(f"{key}={count}" for key, count in offenders.items())
        raise ValueError(
            f"identity column(s) outside the 32 lowercase hex storage form ({detail}); "
            "resolve them before migrating the column"
        )


def _dedash_assignment(column: str) -> str:
    """Return the ``column = replace(...)`` assignment stripping dashes from ``column``."""
    return f"{column} = replace({column}, '-', '')"


def _redash_assignment(column: str) -> str:
    """Return the ``column = substr(...) || ...`` assignment restoring ``column``'s dashed form.

    Correct only for values matching :data:`_UUID_STORAGE_GLOB`, checked first.
    """
    return (
        f"{column} = substr({column}, 1, 8) || '-' || substr({column}, 9, 4) || '-' || "
        f"substr({column}, 13, 4) || '-' || substr({column}, 17, 4) || '-' || substr({column}, 21, 12)"
    )


def upgrade() -> None:
    """Rewrite the values to dashless hex, retype the eight real columns, and leave the FTS index in place.

    Each table's columns are rewritten together in one ``UPDATE`` (see
    :data:`_TABLE_COLUMNS`), so ``graph_mention_cooccurrences``'s
    ``CHECK (mention_id_lo < mention_id_hi)`` never sees a row with one column
    already dashless and the other still dashed.

    Raises:
        ValueError: If the rewrite did not yield the ``sa.Uuid`` storage form for
            every row in every affected table, which means an input was not a
            canonical dashed UUID.
    """
    for table, columns in _TABLE_COLUMNS.items():
        assignments = ", ".join(_dedash_assignment(column) for column in columns)
        op.execute(f"UPDATE {table} SET {assignments}")  # noqa: S608 — table/columns from a fixed allowlist
    _assert_uuid_storage_form()

    with op.batch_alter_table("graph_chunks") as batch:
        batch.alter_column("chunk_id", existing_type=sa.String(), type_=sa.Uuid(), existing_nullable=False)
    with op.batch_alter_table("graph_chunk_run_manifest") as batch:
        batch.alter_column("chunk_id", existing_type=sa.String(), type_=sa.Uuid(), existing_nullable=False)
    with op.batch_alter_table("graph_contextualized_chunks") as batch:
        batch.alter_column("chunk_id", existing_type=sa.String(), type_=sa.Uuid(), existing_nullable=False)
    with op.batch_alter_table("graph_mentions") as batch:
        batch.alter_column("mention_id", existing_type=sa.String(), type_=sa.Uuid(), existing_nullable=False)
        batch.alter_column("chunk_id", existing_type=sa.String(), type_=sa.Uuid(), existing_nullable=False)
    with op.batch_alter_table("graph_mention_cooccurrences") as batch:
        batch.alter_column("mention_id_lo", existing_type=sa.String(), type_=sa.Uuid(), existing_nullable=False)
        batch.alter_column("mention_id_hi", existing_type=sa.String(), type_=sa.Uuid(), existing_nullable=False)
        batch.alter_column("chunk_id", existing_type=sa.String(), type_=sa.Uuid(), existing_nullable=False)


def downgrade() -> None:
    """Restore the string columns and the dashed values everywhere, including the FTS index.

    Each table's columns are re-dashed together in one ``UPDATE``, for the same
    reason :func:`upgrade` rewrites them together — see :data:`_TABLE_COLUMNS`.

    Raises:
        ValueError: If any stored value is outside the ``sa.Uuid`` storage form the
            re-dashing expects.
    """
    _assert_uuid_storage_form()

    with op.batch_alter_table("graph_mention_cooccurrences") as batch:
        batch.alter_column("mention_id_lo", existing_type=sa.Uuid(), type_=sa.String(), existing_nullable=False)
        batch.alter_column("mention_id_hi", existing_type=sa.Uuid(), type_=sa.String(), existing_nullable=False)
        batch.alter_column("chunk_id", existing_type=sa.Uuid(), type_=sa.String(), existing_nullable=False)
    with op.batch_alter_table("graph_mentions") as batch:
        batch.alter_column("mention_id", existing_type=sa.Uuid(), type_=sa.String(), existing_nullable=False)
        batch.alter_column("chunk_id", existing_type=sa.Uuid(), type_=sa.String(), existing_nullable=False)
    with op.batch_alter_table("graph_contextualized_chunks") as batch:
        batch.alter_column("chunk_id", existing_type=sa.Uuid(), type_=sa.String(), existing_nullable=False)
    with op.batch_alter_table("graph_chunk_run_manifest") as batch:
        batch.alter_column("chunk_id", existing_type=sa.Uuid(), type_=sa.String(), existing_nullable=False)
    with op.batch_alter_table("graph_chunks") as batch:
        batch.alter_column("chunk_id", existing_type=sa.Uuid(), type_=sa.String(), existing_nullable=False)

    for table, columns in _TABLE_COLUMNS.items():
        assignments = ", ".join(_redash_assignment(column) for column in columns)
        op.execute(f"UPDATE {table} SET {assignments}")  # noqa: S608 — table/columns from a fixed allowlist
