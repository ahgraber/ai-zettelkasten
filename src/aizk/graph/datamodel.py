"""Graph-stage ORM models: persisted chunks, run manifest/input, and contextualization.

These tables live in the conversion SQLite database alongside the shared
``pipeline_runs`` / ``pipeline_events`` tables (see :mod:`aizk.pipeline`); the
graph stage does not own a separate database. The run / dataset-version
primitive and the transition-event log are reused from :mod:`aizk.pipeline`, so
nothing here redefines a run record — these models carry only the graph-specific
content.

Facts are split by what they are *about*. A :class:`Chunk` is identified by a
stable surrogate and shared across every chunking generation that re-emits it, so
it carries only **stable identity facts** — facts invariant for a ``chunk_id``.
Facts that vary by generation live on the run instead:

- :class:`Chunk` — immutable, run-independent chunk rows whose ``chunk_id`` is a
  stable surrogate assigned once at persistence (reused across generations when
  the sameness-key ``(source_id, heading_path, ordinal, content_hash)`` matches),
  carrying stable facts only (the owning ``source_id``, ``text``,
  ``content_hash``, ``char_count``, ``heading_path``, ``ordinal``). An
  unchanged chunk keeps its identity and its single row across re-chunks. Ordinary
  processing never mutates a row.
- :class:`ChunkRunInput` — one row per chunking run recording what the run
  **consumed**: a locator to the exact Markdown (``conversion_output_id``) and
  that Markdown's ``markdown_hash_xx64`` so the input is retrievable and
  verifiable. Mirrors conversion's input record.
- :class:`ChunkRunManifest` — append-only ``(run_id, chunk_id, span)`` recording
  what the run **produced** and where each chunk sat in that generation's
  Markdown. ``span`` belongs here, not on the shared identity: an unchanged chunk
  keeps its ``chunk_id`` yet shifts offset when a preceding chunk's length
  changes. A ``chunk_id`` is current iff it is in the source's active chunking
  run's manifest; supersession is expressed only on the run's status, never by
  editing or deleting a manifest entry.
- :class:`DocumentSummary` — a run-scoped per-document summary carrying the
  source markdown hash, the consumed ``conversion_output_id`` (provenance), and
  the ``summary_version`` that produced it.
- :class:`ContextualizedChunk` — a run-scoped per-chunk contextualized variant
  storing the model's self-contained **revision** of the chunk (or an empty
  string when the chunk was already self-contained), carrying the source
  ``chunk_id``, ``context_version``, the ``summary_run_id`` and ``chunking_run_id``
  provenance pointers, and a derivation key for the summary text's content
  fingerprint, 2p/1n neighbor content keys, ``splitter_version``, prompt
  identity, and resolved model identity used.
- :class:`ContextualizationOutputMemo` — internal scratch state caching validated
  summary and per-chunk revision model outputs keyed by ``(kind, scope_id,
  derivation_key)`` so a retry of a partially-completed contextualization attempt
  re-invokes the model only for outputs not already retained. Never a product
  projection: a row makes no run, summary, or variant active or readable.
- :class:`Mention` — an append-only, run-scoped entity-mention record produced by
  an extraction run. Carries a surrogate ``mention_id``, the logical ``run_id``,
  a real ``chunk_id`` foreign key into :class:`Chunk`, an ``anchor_kind`` of
  ``source`` (a raw-chunk occurrence, with ``source_span_start``/``source_span_end``
  and ``source_occurrence_key``) or ``revision`` (a contextualization-only
  reference, with no raw span), and the input provenance (``input_kind``,
  ``input_ref``, ``input_span_start``/``input_span_end``) the mention was read from.
- :class:`MentionCooccurrence` — a flat link table recording one row per
  unordered intra-chunk mention pair within a run, with both endpoints foreign
  keying into :class:`Mention` and a canonical ``mention_id_lo < mention_id_hi``
  ordering.
- :class:`ChunkRunApplicability`, :class:`SummaryRunApplicability`,
  :class:`ContextualizationRunApplicability` — append-only records that a run's
  output was also current for another conversion output (chunking, summary) or
  another ``(summary_run_id, chunking_run_id)`` pair (contextualization), so a
  currentness check need not recompute a run's derivation key against every input
  it has ever matched.
- :class:`ContextualizationRunInput`, :class:`ExtractionRunInput` — one row per
  run recording the input/config fingerprints that produced it and (for
  contextualization) its output fingerprint, as :class:`ChunkRunInput` does for
  chunking.

``run_id`` columns are logical references to ``pipeline_runs.id`` with no
database foreign key, matching the runtime's convention so superseded-run
compaction can delete freely. ``chunk_id`` foreign keys into :class:`Chunk`
because it is a genuine same-database identity relationship that downstream
stages (mention extraction) also reference.
"""

from __future__ import annotations

import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlmodel import Field, SQLModel

from aizk.pipeline.lifecycle import WorkUnitStatus

#: The two kinds of contextualization model output the memo retains. The summary
#: output (``summary``) and each per-chunk revision (``revision``) are the only
#: legal discriminator values; a ``CHECK`` constraint on the table and this typed
#: boundary together keep a typo from creating a durable but unreachable entry.
MemoKind = Literal["summary", "revision"]
MEMO_KIND_SUMMARY: MemoKind = "summary"
MEMO_KIND_REVISION: MemoKind = "revision"

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


def _utcnow() -> datetime.datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc)


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
        default_factory=_utcnow,
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
        default_factory=_utcnow,
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


class ContextualizationOutputMemo(SQLModel, table=True):
    """Durable memo of validated model output for resuming partial contextualization.

    The key is ``(kind, scope_id, derivation_key)``. ``kind`` distinguishes summary
    and revision outputs; ``output_text`` stores normalized output, and ``''`` is a
    present empty value distinct from a miss. Memo rows are scratch state, not
    product projections, and do not activate runs or outputs.
    """

    __tablename__ = "graph_contextualization_output_memo"
    __table_args__ = (
        UniqueConstraint(
            "kind",
            "scope_id",
            "derivation_key",
            name="uq_graph_contextualization_output_memo_key",
        ),
        Index(
            "ix_graph_contextualization_output_memo_key",
            "kind",
            "scope_id",
            "derivation_key",
        ),
        # Fail closed on an unknown kind so a typo cannot create a durable but
        # unreachable entry; the spec defines only these two discriminator values.
        CheckConstraint(
            f"kind IN ('{MEMO_KIND_SUMMARY}', '{MEMO_KIND_REVISION}')",
            name="ck_graph_contextualization_output_memo_kind",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    kind: str = Field(nullable=False)
    scope_id: UUID = Field(nullable=False)
    derivation_key: str = Field(sa_column=Column(Text, nullable=False))
    output_text: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime.datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )


class ContextualizationJob(SQLModel, table=True):
    """Claimable work unit for chunk-persist and contextualization of one document.

    ``idempotency_key`` deduplicates enqueue requests. ``conversion_output_id`` is
    a Markdown locator; ``source_id`` scopes the runs and transition events.
    ``status`` and scheduling fields carry runtime claim, lease, and retry state;
    the runtime owns lifecycle transitions.
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
    # values_callable stores enum values ("queued") not names ("QUEUED"), matching RunStatus.
    status: WorkUnitStatus = Field(
        default=WorkUnitStatus.QUEUED,
        sa_column=Column(
            SAEnum(WorkUnitStatus, values_callable=lambda x: [e.value for e in x]),
            nullable=False,
        ),
    )
    attempts: int = Field(default=0, nullable=False)
    error_code: str | None = Field(default=None, nullable=True)
    error_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    earliest_next_attempt_at: datetime.datetime | None = Field(default=None, nullable=True)
    last_error_at: datetime.datetime | None = Field(default=None, nullable=True)
    queued_at: datetime.datetime | None = Field(default=None, nullable=True)
    started_at: datetime.datetime | None = Field(default=None, nullable=True)
    finished_at: datetime.datetime | None = Field(default=None, nullable=True)
    created_at: datetime.datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )
    updated_at: datetime.datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )


class ExtractionJob(SQLModel, table=True):
    """Claimable work unit for extracting one source's mentions and co-occurrence links.

    The row is scoped and deduplicated by ``source_id``. It has no upstream artifact
    locator; extraction resolves upstream runs and applies worker input policy.
    Enqueue reuses terminal rows, while re-extraction requeues the row or uses the
    direct extraction entry points after configuration changes.

    ``status`` and scheduling fields carry runtime claim, lease, and retry state;
    the runtime owns lifecycle transitions.
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
    # values_callable stores enum values ("queued") not names ("QUEUED"), matching RunStatus.
    status: WorkUnitStatus = Field(
        default=WorkUnitStatus.QUEUED,
        sa_column=Column(
            SAEnum(WorkUnitStatus, values_callable=lambda x: [e.value for e in x]),
            nullable=False,
        ),
    )
    attempts: int = Field(default=0, nullable=False)
    error_code: str | None = Field(default=None, nullable=True)
    error_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    earliest_next_attempt_at: datetime.datetime | None = Field(default=None, nullable=True)
    last_error_at: datetime.datetime | None = Field(default=None, nullable=True)
    queued_at: datetime.datetime | None = Field(default=None, nullable=True)
    started_at: datetime.datetime | None = Field(default=None, nullable=True)
    finished_at: datetime.datetime | None = Field(default=None, nullable=True)
    created_at: datetime.datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )
    updated_at: datetime.datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )


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
        default_factory=_utcnow,
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
