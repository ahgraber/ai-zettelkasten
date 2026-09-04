"""Add output-sensitive provenance and applicability tables, plus their output-fingerprint columns.

Revision ID: c5d6e7f8a9b0
Revises: e3f4a5b6c7d8
Create Date: 2026-09-02 12:00:00.000000

Lays the schema foundation for output-sensitive derivation keys:

- ``graph_chunk_run_inputs.chunk_set_fingerprint`` and
  ``graph_document_summaries.summary_text_fingerprint`` — the two producers'
  materialized output fingerprints, added to their existing run-input rows.
- ``graph_chunk_run_applicability`` / ``graph_summary_run_applicability`` — a run's
  output was also current for another ``conversion_output_id``.
- ``graph_contextualization_run_inputs`` — one row per variant run recording its
  ``(summary_run_id, chunking_run_id)`` provenance and its input/config/output
  fingerprints.
- ``graph_contextualization_run_applicability`` — a variant run's output was also
  current for another ``(summary_run_id, chunking_run_id)`` pair.
- ``graph_extraction_run_inputs`` — one row per extraction run recording the
  consumed ``upstream_run_id``, ``input_policy``, and input/config fingerprints.
- ``graph_extraction_run_applicability`` — an extraction run's output was also
  current against another ``upstream_run_id``.

The two new columns are added with ``server_default=""`` so the migration can add
them ``NOT NULL`` against any pre-existing rows; the ORM declares no default
(every row this change's producers write always supplies a real fingerprint), and
the schema-equivalence check between a fresh ``create_all`` and a migrated
database ignores column defaults, only comparing name/type/nullability.

This lives in the conversion Alembic tree, which owns all tables for the single
SQLite database (ADR-003). ``run_id`` columns on every new table are logical
references to ``pipeline_runs.id`` (no foreign key), matching every other
graph-stage run-scoped table.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two output-fingerprint columns and create the six provenance/applicability tables."""
    with op.batch_alter_table("graph_chunk_run_inputs") as batch:
        batch.add_column(sa.Column("chunk_set_fingerprint", sa.String(), nullable=False, server_default=""))
    with op.batch_alter_table("graph_document_summaries") as batch:
        batch.add_column(sa.Column("summary_text_fingerprint", sa.String(), nullable=False, server_default=""))

    op.create_table(
        "graph_chunk_run_applicability",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("conversion_output_id", sa.String(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "conversion_output_id"),
    )
    op.create_index(
        "ix_graph_chunk_run_applicability_conversion_output_id",
        "graph_chunk_run_applicability",
        ["conversion_output_id"],
        unique=False,
    )

    op.create_table(
        "graph_summary_run_applicability",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("conversion_output_id", sa.String(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "conversion_output_id"),
    )
    op.create_index(
        "ix_graph_summary_run_applicability_conversion_output_id",
        "graph_summary_run_applicability",
        ["conversion_output_id"],
        unique=False,
    )

    op.create_table(
        "graph_contextualization_run_inputs",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("summary_run_id", sa.Integer(), nullable=False),
        sa.Column("chunking_run_id", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("config_fingerprint", sa.String(), nullable=False),
        sa.Column("output_fingerprint", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )

    op.create_table(
        "graph_contextualization_run_applicability",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("summary_run_id", sa.Integer(), nullable=False),
        sa.Column("chunking_run_id", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "summary_run_id", "chunking_run_id"),
    )
    op.create_index(
        "ix_graph_contextualization_run_applicability_summary_chunking",
        "graph_contextualization_run_applicability",
        ["summary_run_id", "chunking_run_id"],
        unique=False,
    )

    op.create_table(
        "graph_extraction_run_inputs",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("upstream_run_id", sa.Integer(), nullable=False),
        sa.Column("input_policy", sa.String(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("config_fingerprint", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )

    op.create_table(
        "graph_extraction_run_applicability",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("upstream_run_id", sa.Integer(), nullable=False),
        sa.Column("input_policy", sa.String(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "upstream_run_id"),
    )
    op.create_index(
        "ix_graph_extraction_run_applicability_upstream_run_id",
        "graph_extraction_run_applicability",
        ["upstream_run_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the six tables and the two output-fingerprint columns."""
    op.drop_index(
        "ix_graph_extraction_run_applicability_upstream_run_id", table_name="graph_extraction_run_applicability"
    )
    op.drop_table("graph_extraction_run_applicability")

    op.drop_table("graph_extraction_run_inputs")

    op.drop_index(
        "ix_graph_contextualization_run_applicability_summary_chunking",
        table_name="graph_contextualization_run_applicability",
    )
    op.drop_table("graph_contextualization_run_applicability")

    op.drop_table("graph_contextualization_run_inputs")

    op.drop_index(
        "ix_graph_summary_run_applicability_conversion_output_id", table_name="graph_summary_run_applicability"
    )
    op.drop_table("graph_summary_run_applicability")

    op.drop_index("ix_graph_chunk_run_applicability_conversion_output_id", table_name="graph_chunk_run_applicability")
    op.drop_table("graph_chunk_run_applicability")

    with op.batch_alter_table("graph_document_summaries") as batch:
        batch.drop_column("summary_text_fingerprint")
    with op.batch_alter_table("graph_chunk_run_inputs") as batch:
        batch.drop_column("chunk_set_fingerprint")
