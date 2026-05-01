"""Owner-scoped uniqueness for conversion_jobs.idempotency_key.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-05-01 00:00:00.000000

Replace the legacy global-unique index on ``conversion_jobs.idempotency_key``
with owner-scoped uniqueness on ``(owner_id, idempotency_key)``. The
``idempotency_key`` column keeps a non-unique index so lookups stay cheap.

Two different principals may legitimately submit the same source/config and
compute the same key; the new shape lets each own its own row while still
preventing same-owner duplicates. ``downgrade()`` is conditional: if any
post-upgrade rows share an ``idempotency_key`` across distinct ``owner_id``
values, restoring the global-unique shape would destroy data, so the
migration aborts with ``IrreversibleMigrationError`` before any schema
change.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from aizk.conversion.core.errors import IrreversibleMigrationError

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the global unique index on ``idempotency_key`` and add an owner-scoped composite."""
    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversion_jobs_idempotency_key"))
        batch_op.create_index(
            batch_op.f("ix_conversion_jobs_idempotency_key"),
            ["idempotency_key"],
            unique=False,
        )
        batch_op.create_index(
            "uq_conversion_jobs_owner_idempotency_key",
            ["owner_id", "idempotency_key"],
            unique=True,
        )


def downgrade() -> None:
    """Restore the global-unique index, aborting if cross-owner duplicates would be lost."""
    conn = op.get_bind()
    duplicate_count = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM ("
            "  SELECT idempotency_key FROM conversion_jobs"
            "  GROUP BY idempotency_key"
            "  HAVING COUNT(DISTINCT owner_id) > 1"
            ") AS conflicts"
        )
    ).scalar()
    if duplicate_count:
        raise IrreversibleMigrationError(
            "Cannot restore global-unique idempotency_key: "
            f"{duplicate_count} idempotency_key value(s) are held by multiple owner_id values; "
            "downgrade would discard valid post-upgrade data"
        )

    with op.batch_alter_table("conversion_jobs", schema=None) as batch_op:
        batch_op.drop_index("uq_conversion_jobs_owner_idempotency_key")
        batch_op.drop_index(batch_op.f("ix_conversion_jobs_idempotency_key"))
        batch_op.create_index(
            batch_op.f("ix_conversion_jobs_idempotency_key"),
            ["idempotency_key"],
            unique=True,
        )
