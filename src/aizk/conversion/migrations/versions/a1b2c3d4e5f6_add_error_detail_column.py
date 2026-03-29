"""Add error_detail column to conversion_jobs.

Revision ID: a1b2c3d4e5f6
Revises: 57317cf19d3b
Create Date: 2026-03-29 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "57317cf19d3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply schema changes."""
    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("error_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    """Revert schema changes."""
    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.drop_column("error_detail")
