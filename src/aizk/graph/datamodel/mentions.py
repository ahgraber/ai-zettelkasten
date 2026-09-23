"""Mention, co-occurrence, extraction-run input, and applicability tables with their anchor and input kinds."""

from __future__ import annotations

import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Text, func, text
from sqlmodel import Field, SQLModel

from aizk.utilities.clock import utcnow

#: A mention's raw-provenance class: ``source`` for a mention whose surface form
#: occurs in the raw chunk text (carrying ``source_span_start``/``source_span_end``),
#: ``revision`` for a mention resolved only by the contextualized revision (no raw
#: span). A ``CHECK`` constraint on the table and this typed boundary together keep
#: a typo from creating a durable but unreachable anchor class.
AnchorKind = Literal["source", "revision"]
ANCHOR_KIND_SOURCE: AnchorKind = "source"
ANCHOR_KIND_REVISION: AnchorKind = "revision"

#: The text a mention was read from: ``raw`` for the persisted chunk text,
#: ``contextualized`` for an active contextualized variant. A mention read from
#: ``raw`` input is always ``source``-anchored (validated at the persistence
#: boundary, not by ``CHECK``).
InputKind = Literal["raw", "contextualized"]
INPUT_KIND_RAW: InputKind = "raw"
INPUT_KIND_CONTEXTUALIZED: InputKind = "contextualized"


class ExtractionRunInput(SQLModel, table=True):
    """Provenance and fingerprints recorded for one extraction run.

    ``upstream_run_id`` identifies the consumed upstream output. ``input_fingerprint``
    covers the chunk texts selected by ``input_policy``; ``config_fingerprint``
    covers extractor, materializer, and policy versions.
    """

    __tablename__ = "graph_extraction_run_inputs"

    run_id: int = Field(primary_key=True, nullable=False)
    upstream_run_id: int = Field(nullable=False)
    input_policy: str = Field(nullable=False)
    input_fingerprint: str = Field(nullable=False)
    config_fingerprint: str = Field(nullable=False)


class ExtractionRunApplicability(SQLModel, table=True):
    """Records that an extraction run is current against another upstream run.

    Rows are append-only and idempotent on ``(run_id, upstream_run_id)``.
    """

    __tablename__ = "graph_extraction_run_applicability"
    __table_args__ = (Index("ix_graph_extraction_run_applicability_upstream_run_id", "upstream_run_id"),)

    run_id: int = Field(primary_key=True, nullable=False)
    upstream_run_id: int = Field(primary_key=True, nullable=False)
    input_policy: str = Field(nullable=False)
    input_fingerprint: str = Field(nullable=False)


class Mention(SQLModel, table=True):
    """Append-only, run-scoped entity mention.

    ``mention_id`` is a persistence-assigned UUID. ``run_id`` identifies the
    extraction run and ``chunk_id`` identifies the source chunk.

    ``anchor_kind`` is ``source`` when the surface form has a raw span and
    ``revision`` when contextualized text resolves the mention without a raw span.
    Raw input always uses ``source`` anchoring. ``input_kind``, ``input_ref``, and
    input spans identify the exact text read; ``input_ref`` is canonical JSON and
    may include the contextualization run ID.

    Partial unique indexes enforce one raw mention per occurrence and one revision
    mention per chunk and surface form.
    """

    __tablename__ = "graph_mentions"
    __table_args__ = (
        Index("ix_graph_mentions_run_id", "run_id"),
        Index("ix_graph_mentions_chunk_id", "chunk_id"),
        CheckConstraint(
            f"anchor_kind IN ('{ANCHOR_KIND_SOURCE}', '{ANCHOR_KIND_REVISION}')",
            name="ck_graph_mentions_anchor_kind",
        ),
        CheckConstraint(
            f"input_kind IN ('{INPUT_KIND_RAW}', '{INPUT_KIND_CONTEXTUALIZED}')",
            name="ck_graph_mentions_input_kind",
        ),
        CheckConstraint(
            f"anchor_kind != '{ANCHOR_KIND_SOURCE}' OR "
            "(source_span_start IS NOT NULL AND source_span_end IS NOT NULL "
            "AND source_occurrence_key IS NOT NULL)",
            name="ck_graph_mentions_source_anchor_fields",
        ),
        CheckConstraint(
            f"anchor_kind != '{ANCHOR_KIND_REVISION}' OR "
            "(source_span_start IS NULL AND source_span_end IS NULL "
            "AND source_occurrence_key IS NULL)",
            name="ck_graph_mentions_revision_anchor_fields",
        ),
        # Source-anchored within-run identity: one mention per raw occurrence.
        Index(
            "uq_graph_mentions_source_identity",
            "run_id",
            "chunk_id",
            "source_span_start",
            "source_span_end",
            "surface_form",
            unique=True,
            sqlite_where=text(f"anchor_kind = '{ANCHOR_KIND_SOURCE}'"),
            postgresql_where=text(f"anchor_kind = '{ANCHOR_KIND_SOURCE}'"),
        ),
        # Revision-anchored within-run identity: one mention per (chunk, surface).
        # A separate index (not the source one above) because a nullable-span index
        # would not deduplicate these rows — see the class docstring.
        Index(
            "uq_graph_mentions_revision_identity",
            "run_id",
            "chunk_id",
            "surface_form",
            unique=True,
            sqlite_where=text(f"anchor_kind = '{ANCHOR_KIND_REVISION}'"),
            postgresql_where=text(f"anchor_kind = '{ANCHOR_KIND_REVISION}'"),
        ),
    )

    mention_id: UUID = Field(primary_key=True, nullable=False)
    run_id: int = Field(nullable=False)
    chunk_id: UUID = Field(sa_column=Column(ForeignKey("graph_chunks.chunk_id"), nullable=False))
    anchor_kind: str = Field(nullable=False)
    surface_form: str = Field(sa_column=Column(Text, nullable=False))
    input_kind: str = Field(nullable=False)
    input_ref: str = Field(sa_column=Column(Text, nullable=False))
    input_span_start: int = Field(nullable=False)
    input_span_end: int = Field(nullable=False)
    source_span_start: int | None = Field(default=None, nullable=True)
    source_span_end: int | None = Field(default=None, nullable=True)
    source_occurrence_key: str | None = Field(default=None, nullable=True)
    created_at: datetime.datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )


class MentionCooccurrence(SQLModel, table=True):
    """Unordered pair of mentions from one chunk and extraction run.

    The primary key ``(run_id, mention_id_lo, mention_id_hi)`` and
    ``mention_id_lo < mention_id_hi`` enforce one canonical pair and exclude
    self-pairs. Both endpoints reference :class:`Mention`; ``chunk_id`` records
    the shared chunk.
    """

    __tablename__ = "graph_mention_cooccurrences"
    __table_args__ = (
        CheckConstraint("mention_id_lo < mention_id_hi", name="ck_graph_mention_cooccurrences_ordered"),
        Index("ix_graph_mention_cooccurrences_chunk_id", "chunk_id"),
        Index("ix_graph_mention_cooccurrences_hi", "mention_id_hi"),
    )

    run_id: int = Field(primary_key=True, nullable=False)
    mention_id_lo: UUID = Field(
        sa_column=Column(ForeignKey("graph_mentions.mention_id"), primary_key=True, nullable=False)
    )
    mention_id_hi: UUID = Field(
        sa_column=Column(ForeignKey("graph_mentions.mention_id"), primary_key=True, nullable=False)
    )
    chunk_id: UUID = Field(nullable=False)
