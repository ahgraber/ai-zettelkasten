"""Add owner_id columns to sources, conversion_jobs, and conversion_outputs.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-04-28 00:00:00.000000

Three-phase column transition matching the source_ref/source_ref_hash
NOT NULL precedent: add NULLABLE -> backfill -> assert no NULLs ->
ALTER NOT NULL. The backfill value is `AIZK_DEFAULT_PRINCIPAL` resolved
once at migration start so a value rotation mid-migration cannot produce
inconsistent attribution across the three tables.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from aizk.conversion.core.errors import IrreversibleMigrationError
from aizk.conversion.utilities.config import AuthSettings

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES: tuple[str, ...] = ("sources", "conversion_jobs", "conversion_outputs")


def upgrade() -> None:
    """Add `owner_id` to all three tables; backfill from AIZK_DEFAULT_PRINCIPAL; finalize NOT NULL with index."""
    # Snapshot the deployment's default principal once so a mid-migration env-var
    # rotation cannot produce inconsistent attribution across the three tables.
    default_principal_value = AuthSettings().default_principal

    for table in _TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("owner_id", sa.Text(), nullable=True))

    conn = op.get_bind()
    for table in _TABLES:
        conn.execute(
            sa.text(f"UPDATE {table} SET owner_id = :value WHERE owner_id IS NULL"),
            {"value": default_principal_value},
        )

    null_counts = {
        table: conn.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE owner_id IS NULL")).scalar()
        for table in _TABLES
    }
    offenders = {table: count for table, count in null_counts.items() if count}
    if offenders:
        raise IrreversibleMigrationError(
            "Cannot enforce NOT NULL on owner_id: backfill left NULL rows in "
            + ", ".join(f"{table}={count}" for table, count in offenders.items())
        )

    for table in _TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column("owner_id", existing_type=sa.Text(), nullable=False)
            batch_op.create_index(batch_op.f(f"ix_{table}_owner_id"), ["owner_id"], unique=False)


def downgrade() -> None:
    """Drop the three owner_id indexes and columns; row data is preserved."""
    for table in _TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(batch_op.f(f"ix_{table}_owner_id"))
            batch_op.drop_column("owner_id")
