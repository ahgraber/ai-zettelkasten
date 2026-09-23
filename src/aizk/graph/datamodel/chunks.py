"""Chunk identity, chunking-run input, applicability, and manifest tables."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Column, ForeignKey, Index, Text
from sqlmodel import Field, SQLModel


class Chunk(SQLModel, table=True):
    """Persisted structural chunk with stable identity.

    Each row represents one ``(source_id, heading_path, ordinal, content_hash)``
    sameness key. ``chunk_id`` is reused across runs and rows are immutable.
    Generation-varying Markdown hash, splitter version, and span belong to run
    tables. ``heading_path_json`` stores the heading path as a canonical JSON array.
    """

    __tablename__ = "graph_chunks"
    __table_args__ = (
        Index("ix_graph_chunks_source_id", "source_id"),
        # The sameness-key: persistence reuses the existing surrogate ``chunk_id``
        # when this tuple is already present and mints a new one otherwise, so the
        # uniqueness is the database-level backing of run-independent identity reuse.
        Index(
            "ix_graph_chunks_sameness_key",
            "source_id",
            "heading_path_json",
            "ordinal",
            "content_hash",
            unique=True,
        ),
    )

    chunk_id: UUID = Field(primary_key=True, nullable=False)
    content_hash: str = Field(nullable=False)
    source_id: UUID = Field(nullable=False)
    heading_path_json: str = Field(sa_column=Column(Text, nullable=False))
    ordinal: int = Field(nullable=False)
    text: str = Field(sa_column=Column(Text, nullable=False))
    char_count: int = Field(nullable=False)


class ChunkRunInput(SQLModel, table=True):
    """Input provenance for a chunking run.

    Stores the conversion-output locator, Markdown hash, and emitted chunk-set
    fingerprint. ``conversion_output_id`` is a string locator, not a derivation
    input; ``run_id`` is a logical reference to ``pipeline_runs.id``.
    """

    __tablename__ = "graph_chunk_run_inputs"

    run_id: int = Field(primary_key=True, nullable=False)
    conversion_output_id: str = Field(nullable=False)
    markdown_hash_xx64: str = Field(nullable=False)
    #: The run's output fingerprint: ``payload_fingerprint`` over the emitted
    #: chunk set, in document order (see ``aizk.graph.fingerprints``). A
    #: downstream currentness check reads this column instead of recomputing it.
    chunk_set_fingerprint: str = Field(nullable=False)


class ChunkRunApplicability(SQLModel, table=True):
    """Records that a chunking run is current for another conversion output.

    Rows are append-only and idempotent on ``(run_id, conversion_output_id)``.
    """

    __tablename__ = "graph_chunk_run_applicability"
    __table_args__ = (Index("ix_graph_chunk_run_applicability_conversion_output_id", "conversion_output_id"),)

    run_id: int = Field(primary_key=True, nullable=False)
    conversion_output_id: str = Field(primary_key=True, nullable=False)
    input_fingerprint: str = Field(nullable=False)


class ChunkRunManifest(SQLModel, table=True):
    """Append-only chunk membership and span for a chunking run.

    The composite key ``(run_id, chunk_id)`` makes membership idempotent. Spans
    belong to the generation, while ``chunk_id`` identifies shared chunk data.
    """

    __tablename__ = "graph_chunk_run_manifest"
    __table_args__ = (Index("ix_graph_chunk_run_manifest_chunk_id", "chunk_id"),)

    run_id: int = Field(primary_key=True, nullable=False)
    chunk_id: UUID = Field(
        sa_column=Column(
            ForeignKey("graph_chunks.chunk_id"),
            primary_key=True,
            nullable=False,
        )
    )
    span_start: int = Field(nullable=False)
    span_end: int = Field(nullable=False)
