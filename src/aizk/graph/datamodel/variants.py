"""Contextualized chunk variant, contextualization-run input, and applicability tables."""

from __future__ import annotations

import datetime
from uuid import UUID

from sqlalchemy import Column, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlmodel import Field, SQLModel

from aizk.utilities.clock import utcnow


class ContextualizedChunk(SQLModel, table=True):
    """Run-scoped contextualized variant for one chunk.

    ``contextualized_text`` contains a self-contained revision, or an empty string
    when the raw chunk is already self-contained. The revision does not modify the
    source :class:`Chunk` row.

    ``summary_run_id`` and ``chunking_run_id`` identify the upstream runs used for
    provenance. ``derivation_key`` uses content-derived inputs and excludes local
    run IDs.
    """

    __tablename__ = "graph_contextualized_chunks"
    __table_args__ = (
        UniqueConstraint("run_id", "chunk_id", name="uq_graph_contextualized_chunks_run_chunk"),
        Index("ix_graph_contextualized_chunks_run_id", "run_id"),
        Index("ix_graph_contextualized_chunks_chunk_id", "chunk_id"),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    run_id: int = Field(nullable=False)
    summary_run_id: int = Field(nullable=False)
    chunking_run_id: int = Field(nullable=False)
    chunk_id: UUID = Field(sa_column=Column(ForeignKey("graph_chunks.chunk_id"), nullable=False))
    context_version: int = Field(nullable=False)
    contextualized_text: str = Field(sa_column=Column(Text, nullable=False))
    derivation_key: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime.datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )


class ContextualizationRunInput(SQLModel, table=True):
    """Provenance and fingerprints for a contextualization run.

    Records upstream run IDs, input/config fingerprints, and output fingerprint.
    The row is written once per run, including zero-chunk runs.
    """

    __tablename__ = "graph_contextualization_run_inputs"

    run_id: int = Field(primary_key=True, nullable=False)
    summary_run_id: int = Field(nullable=False)
    chunking_run_id: int = Field(nullable=False)
    input_fingerprint: str = Field(nullable=False)
    config_fingerprint: str = Field(nullable=False)
    output_fingerprint: str = Field(nullable=False)


class ContextualizationRunApplicability(SQLModel, table=True):
    """Records that a variant run is current for another summary/chunking pair.

    Rows are append-only and idempotent on
    ``(run_id, summary_run_id, chunking_run_id)``.
    """

    __tablename__ = "graph_contextualization_run_applicability"
    __table_args__ = (
        Index(
            "ix_graph_contextualization_run_applicability_summary_chunking",
            "summary_run_id",
            "chunking_run_id",
        ),
    )

    run_id: int = Field(primary_key=True, nullable=False)
    summary_run_id: int = Field(primary_key=True, nullable=False)
    chunking_run_id: int = Field(primary_key=True, nullable=False)
    input_fingerprint: str = Field(nullable=False)
