"""Add composite index on status, earliest_next_attempt_at, queued_at.

Revision ID: b7f8e9a0c1d2
Revises: a1b2c3d4e5f6
Create Date: 2026-03-30 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7f8e9a0c1d2"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add composite index for job selection and queue depth queries."""
    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_conversion_jobs_status_next_attempt_queued",
            ["status", "earliest_next_attempt_at", "queued_at"],
            unique=False,
        )


def downgrade() -> None:
    """Remove composite index."""
    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_conversion_jobs_status_next_attempt_queued")
