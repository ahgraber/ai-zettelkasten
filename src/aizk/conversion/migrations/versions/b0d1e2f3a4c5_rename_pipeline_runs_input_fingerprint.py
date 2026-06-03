"""Rename ``pipeline_runs.input_fingerprint`` to ``derivation_key``.

Revision ID: b0d1e2f3a4c5
Revises: a9c0d1e2f3b4
Create Date: 2026-06-02 14:00:00.000000

The shared stage-run primitive (``d0e1f2a3b4c5``) shipped the column as
``input_fingerprint``. The chunk-persistence-contextualization change adopts the
``derivation_key`` vocabulary (a reproducible key over the inputs/configuration
that produced a run's derived outputs), which the ``PipelineRun`` ORM now
expects. This forward migration renames the column in place so databases already
at ``d0e1f2a3b4c5`` are migrated rather than left diverging from the ORM —
the earlier migration is **not** edited in place (Alembic would not re-run it).

``ALTER TABLE ... RENAME COLUMN`` is native on SQLite (3.25+) and Postgres and
preserves the table's data, indexes (including the partial unique active-run
index), and constraints without a table rebuild.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b0d1e2f3a4c5"
down_revision: str | None = "a9c0d1e2f3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename ``input_fingerprint`` to ``derivation_key`` on ``pipeline_runs``."""
    op.execute("ALTER TABLE pipeline_runs RENAME COLUMN input_fingerprint TO derivation_key")


def downgrade() -> None:
    """Rename ``derivation_key`` back to ``input_fingerprint`` on ``pipeline_runs``."""
    op.execute("ALTER TABLE pipeline_runs RENAME COLUMN derivation_key TO input_fingerprint")
