"""Contextualization and extraction work-unit tables."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field

from aizk.pipeline.lifecycle import WorkUnitColumns


class ContextualizationJob(WorkUnitColumns, table=True):
    """Claimable work unit for chunk-persist and contextualization of one document.

    ``idempotency_key`` deduplicates enqueue requests. ``conversion_output_id`` is
    a Markdown locator; ``source_id`` scopes the runs and transition events.
    The lifecycle columns come from :class:`WorkUnitColumns`.
    """

    __tablename__ = "graph_contextualization_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_graph_contextualization_jobs_idempotency_key"),
        Index(
            "ix_graph_contextualization_jobs_claim",
            "status",
            "earliest_next_attempt_at",
            "queued_at",
        ),
        Index("ix_graph_contextualization_jobs_source_id", "source_id"),
        Index("ix_graph_contextualization_jobs_conversion_output_id", "conversion_output_id"),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    idempotency_key: str = Field(nullable=False)
    conversion_output_id: int = Field(nullable=False)
    source_id: UUID = Field(nullable=False)


class ExtractionJob(WorkUnitColumns, table=True):
    """Claimable work unit for extracting one source's mentions and co-occurrence links.

    The row is scoped and deduplicated by ``source_id``. It has no upstream artifact
    locator; extraction resolves upstream runs and applies worker input policy.
    Enqueue reuses terminal rows, while re-extraction requeues the row or uses the
    direct extraction entry points after configuration changes.

    The lifecycle columns come from :class:`WorkUnitColumns`.
    """

    __tablename__ = "graph_extraction_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_graph_extraction_jobs_idempotency_key"),
        Index(
            "ix_graph_extraction_jobs_claim",
            "status",
            "earliest_next_attempt_at",
            "queued_at",
        ),
        Index("ix_graph_extraction_jobs_source_id", "source_id"),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    idempotency_key: str = Field(nullable=False)
    source_id: UUID = Field(nullable=False)
