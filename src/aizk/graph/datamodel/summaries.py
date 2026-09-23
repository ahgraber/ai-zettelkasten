"""Document summary and summary-run applicability tables."""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, Index, Text, UniqueConstraint, func
from sqlmodel import Field, SQLModel

from aizk.utilities.clock import utcnow


class DocumentSummary(SQLModel, table=True):
    """Run-scoped summary for one document.

    Stores the source hash and conversion-output locator as provenance, plus the
    summary version and text. The active run's derivation key controls reuse and
    supersession; this row remains immutable.
    """

    __tablename__ = "graph_document_summaries"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_graph_document_summaries_run_id"),
        Index("ix_graph_document_summaries_run_id", "run_id"),
        Index("ix_graph_document_summaries_conversion_output_id", "conversion_output_id"),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    run_id: int = Field(nullable=False)
    conversion_output_id: str = Field(nullable=False)
    summary_text: str = Field(sa_column=Column(Text, nullable=False))
    markdown_hash_xx64: str = Field(nullable=False)
    summary_version: int = Field(nullable=False)
    #: The run's output fingerprint: ``content_fingerprint(summary_text)``. A
    #: downstream currentness check reads this column instead of recomputing it.
    summary_text_fingerprint: str = Field(nullable=False)
    created_at: datetime.datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )


class SummaryRunApplicability(SQLModel, table=True):
    """Records that a summary run is current for another conversion output.

    Rows are append-only and idempotent on ``(run_id, conversion_output_id)``.
    """

    __tablename__ = "graph_summary_run_applicability"
    __table_args__ = (Index("ix_graph_summary_run_applicability_conversion_output_id", "conversion_output_id"),)

    run_id: int = Field(primary_key=True, nullable=False)
    conversion_output_id: str = Field(primary_key=True, nullable=False)
    input_fingerprint: str = Field(nullable=False)
