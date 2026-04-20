"""Enforce NOT NULL on sources.source_ref and sources.source_ref_hash.

Revision ID: d5e6f7a8b9c0
Revises: c1d2e3f4a5b6
Create Date: 2026-04-20 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from aizk.conversion.core.errors import IrreversibleMigrationError

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    null_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM sources WHERE source_ref IS NULL OR source_ref_hash IS NULL")
    ).scalar()
    if null_count:
        raise IrreversibleMigrationError(
            f"Cannot enforce NOT NULL: {null_count} row(s) in sources have NULL source_ref or source_ref_hash. "
            "Backfill or delete those rows before running this migration."
        )

    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.alter_column("source_ref", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("source_ref_hash", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.alter_column("source_ref", existing_type=sa.Text(), nullable=True)
        batch_op.alter_column("source_ref_hash", existing_type=sa.Text(), nullable=True)
