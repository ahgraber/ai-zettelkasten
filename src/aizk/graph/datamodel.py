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
  carrying stable facts only (the source ``source_id`` ``= str(source_id)``,
  ``text``, ``content_hash``, ``char_count``, ``heading_path``, ``ordinal``). An
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
  provenance pointers, and a derivation key for the summary, 2p/1n neighbor
  identities, ``splitter_version``, prompt identity, and model profile used.
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
    """A persisted structural chunk emitted by the splitter, identified by a stable surrogate.

    One row per distinct sameness-key ``(source_id, heading_path, ordinal,
    content_hash)`` — enforced by the ``ix_graph_chunks_sameness_key`` unique
    index — carrying **stable identity facts only**. ``chunk_id`` is a surrogate
    assigned once at persistence, never content-derived; the ``content_hash``
    survives as a separate observable column. Rows are immutable and
    run-independent: persisting a chunk whose sameness-key already exists reuses
    the row (and its surrogate) unchanged, and the same ``chunk_id`` is shared
    across every chunking run it appears in via :class:`ChunkRunManifest`.

    Generation-varying facts — the source ``markdown_hash_xx64``, the
    ``splitter_version``, and the chunk's ``span`` in that generation's markdown —
    are deliberately **not** stored here; they live on :class:`ChunkRunInput` and
    :class:`ChunkRunManifest`. Storing them on the shared row would be a
    first-writer lie: a chunk re-emitted unchanged by a later generation would
    report whichever generation wrote the row first. ``heading_path`` (a tuple) is
    stored as a canonical JSON array in ``heading_path_json``; the persistence
    layer owns the lossless mapping back to the in-memory contract.
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

    chunk_id: str = Field(primary_key=True, nullable=False)
    content_hash: str = Field(nullable=False)
    source_id: str = Field(nullable=False)
    heading_path_json: str = Field(sa_column=Column(Text, nullable=False))
    ordinal: int = Field(nullable=False)
    text: str = Field(sa_column=Column(Text, nullable=False))
    char_count: int = Field(nullable=False)


class ChunkRunInput(SQLModel, table=True):
    """The Markdown a chunking run consumed: a retrievable locator plus its hash.

    One row per chunking run (``run_id`` primary key). Records the
    ``conversion_output_id`` locator for the exact Markdown the run read and that
    Markdown's ``markdown_hash_xx64`` so the input is both retrievable and
    verifiable after the run is superseded — recovering what a generation
    consumed without re-running the splitter. ``run_id`` is a logical reference to
    ``pipeline_runs.id`` (no foreign key) so superseded-run compaction can delete
    freely.

    ``conversion_output_id`` is the **stringified** form of the conversion
    artifact locator (``ConversionOutput.id``, an integer PK). It is stored as
    text — matching the splitter's ``converted_artifact_id`` contract — to keep
    the graph schema decoupled from the conversion stage's PK type and portable
    along ADR-003's Postgres path; a caller dereferencing it back to a
    ``ConversionOutput`` row converts to the native id type at that boundary. It
    is a locator, never a derivation input.
    """

    __tablename__ = "graph_chunk_run_inputs"

    run_id: int = Field(primary_key=True, nullable=False)
    conversion_output_id: str = Field(nullable=False)
    markdown_hash_xx64: str = Field(nullable=False)


class ChunkRunManifest(SQLModel, table=True):
    """Append-only manifest of the chunks a chunking run produced, with their spans.

    The composite primary key ``(run_id, chunk_id)`` makes re-persisting a chunk
    within the same run idempotent (no duplicate entry) and never mutates an
    existing row. ``span_start`` / ``span_end`` capture where the chunk sat in
    *this* generation's markdown — a generation-varying fact that does not belong
    on the shared content-addressed identity. ``run_id`` is a logical reference to
    ``pipeline_runs.id`` (no foreign key); ``chunk_id`` foreign keys into
    :class:`Chunk`.
    """

    __tablename__ = "graph_chunk_run_manifest"
    __table_args__ = (Index("ix_graph_chunk_run_manifest_chunk_id", "chunk_id"),)

    run_id: int = Field(primary_key=True, nullable=False)
    chunk_id: str = Field(
        sa_column=Column(
            ForeignKey("graph_chunks.chunk_id"),
            primary_key=True,
            nullable=False,
        )
    )
    span_start: int = Field(nullable=False)
    span_end: int = Field(nullable=False)


class DocumentSummary(SQLModel, table=True):
    """A run-scoped per-document summary produced by one LLM pass over the document.

    Carries the source markdown hash, the consumed ``conversion_output_id`` as
    provenance (so the summary's input is retrievable as well as verifiable), and
    the ``summary_version``; the owning run carries the derivation key that
    produced it (source markdown hash, prompt identity, model profile, and
    version) and is scoped to the durable source identity (``str(source_id)``).
    Re-summarizing a document whose derivation-key inputs are unchanged reuses the
    active summary run; a change to any opens a new run that supersedes the prior,
    leaving this row present and unmodified.
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
    created_at: datetime.datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )


class ContextualizedChunk(SQLModel, table=True):
    """A run-scoped per-chunk contextualized variant: the model's self-contained revision.

    Holds the dereferencing revision the model produced — the working chunk
    rewritten so every outside reference is resolved inline (``contextualized_text``)
    — or an empty string when the chunk was already self-contained, in which case
    the raw chunk is consumed unchanged. The revision is a separate, derived
    artifact: it is separately addressable from the chunk (its own ``id``) and
    never mutates the source :class:`Chunk` row, which stays the cited,
    source-faithful unit.

    ``derivation_key`` records the contextualization inputs — the summary
    identity, the working chunk's content key, the 2p/1n neighboring chunks'
    content keys (the portable sameness-key fingerprints, not the surrogate
    ``chunk_id``s), the ``splitter_version`` of the chunking generation read, the
    context-window policy, prompt identity, and model profile used to build the
    variant — alongside ``context_version``. A change to any derivation-key input or to
    ``context_version`` opens a new run whose variant supersedes the prior;
    unchanged inputs and version reuse the active run.

    ``summary_run_id`` and ``chunking_run_id`` are provenance-only pointers (to
    the ``pipeline_runs`` rows of the summary read and the exact chunking
    generation whose manifest was read) — direct joins for operator/debug lookup
    and the backward-trace chain. ``chunking_run_id`` disambiguates which
    generation the variant read, since a ``chunk_id`` may appear in many. Both are
    local run locators, so they are deliberately kept **out** of
    ``derivation_key``; reuse/supersession is decided by the content-derived
    identities the key carries, not by these row ids.
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
    chunk_id: str = Field(sa_column=Column(ForeignKey("graph_chunks.chunk_id"), nullable=False))
    context_version: int = Field(nullable=False)
    contextualized_text: str = Field(sa_column=Column(Text, nullable=False))
    derivation_key: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime.datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )


class ContextualizationOutputMemo(SQLModel, table=True):
    """A durable checkpoint of a validated contextualization model output.

    Internal scratch state for resuming a partially-completed contextualization
    attempt: it caches the validated summary and per-chunk revision outputs keyed
    by their input-deterministic derivation keys, so a retry re-invokes the model
    only for outputs not already retained. It is **not** a product projection — the
    operator and explorer surfaces read :class:`DocumentSummary` /
    :class:`ContextualizedChunk`, never this table — and a row never makes a run,
    summary, or variant active or readable.

    Identity is ``(kind, scope_id, derivation_key)``:

    - ``kind`` discriminates the summary output (``summary``) from a per-chunk
      revision (``revision``); a single discriminated table avoids two near-identical
      tables, and the keys never collide across kinds because their JSON shapes
      differ and ``kind`` partitions them.
    - ``scope_id`` is the durable source identity (``str(source_id)``). It is
      load-bearing for the summary kind, whose derivation key does **not** embed the
      source: without it two distinct sources with byte-identical Markdown would
      share a summary entry. The revision key is already source-distinct via its
      ``chunk_id``, so ``scope_id`` is redundant-but-harmless there and keeps the
      schema uniform; it also makes the success-path prune key set well-defined per
      source.
    - ``derivation_key`` is the respective input-deterministic derivation key — the
      summary derivation key for ``summary``, the per-row variant derivation key for
      ``revision``.

    ``output_text`` stores the validated, normalized model output; ``''`` is a legal
    value meaning the model judged the chunk already self-contained — a present-empty
    entry that is a reuse hit, distinct from an absent entry (a miss that
    re-invokes the model). ``created_at`` is recorded so a later permanent-failure /
    TTL sweep is actionable.
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
    scope_id: str = Field(nullable=False)
    derivation_key: str = Field(sa_column=Column(Text, nullable=False))
    output_text: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime.datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )


class ContextualizationJob(SQLModel, table=True):
    """A claimable work-unit: chunk-persist and contextualize one converted document.

    Mirrors the conversion stage's ``conversion_jobs`` — one row per document to
    process, scoped to the durable source identity ``source_id`` — so the shared
    runtime's claim/lease/retry/stale-recovery machinery can drive the graph stage
    the same way it drives conversion. The runtime owns the lifecycle transitions
    (this change only enqueues rows and runs the unit-of-work); the stage adapter
    that claims and finalizes units is a separate concern.

    Identity vocabulary follows the conversion pattern:

    - ``id`` is the local claim handle (a row surrogate), never a derivation input.
    - ``idempotency_key`` deduplicates enqueue requests, so re-enqueueing the same
      document (incremental re-ingest, or a backfill overlapping an open unit)
      reuses the existing row instead of creating a second.
    - ``conversion_output_id`` is the local artifact locator used to fetch the
      Markdown the unit splits and persists; it is a locator, never a derivation
      input. ``source_id`` is the durable source identity carried onto the runs
      and transition events. Both are stored as plain (logical) references rather
      than cross-stage foreign keys, matching the graph stage's run-reference
      convention and keeping the work-unit table self-contained.

    ``status`` is the runtime's generic :class:`~aizk.pipeline.lifecycle.WorkUnitStatus`;
    ``attempts`` and the ``*_at`` scheduling columns carry the retry/lease
    bookkeeping the runner reads.
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


class Mention(SQLModel, table=True):
    """An append-only, run-scoped entity-mention record produced by an extraction run.

    ``mention_id`` is a surrogate assigned at persistence (a fresh UUID, never
    content-derived), matching the :class:`Chunk` ``chunk_id`` convention: a
    content-derived identity would collide across extraction runs, and a run-local
    identity would not be portable across a logical migration. ``run_id`` is a
    logical reference to ``pipeline_runs.id`` (no foreign key, so superseded-run
    compaction can delete freely); ``chunk_id`` is a real foreign key into
    :class:`Chunk`, making "a mention's source chunk is resolvable" a database
    invariant.

    ``anchor_kind`` partitions mentions into two classes. A ``source``-anchored
    mention's surface form occurs in the raw chunk text: it carries
    ``source_span_start`` / ``source_span_end`` (the occurrence's offsets in the raw
    chunk) and ``source_occurrence_key`` (a cross-run-stable diagnostic hash of that
    occurrence). A ``revision``-anchored mention was resolved only by the
    contextualized revision and does not occur verbatim in the raw chunk: it carries
    no raw span or occurrence key, and its raw provenance is the chunk itself plus
    its recorded input. A mention read from raw input (``input_kind = raw``) is
    always ``source``-anchored; this implication is validated at the persistence
    boundary, not by a ``CHECK`` constraint, since it spans two columns' legal value
    sets rather than one row's internal consistency.

    ``input_kind`` / ``input_ref`` / ``input_span_start`` / ``input_span_end``
    record the exact text the mention was read from (the raw chunk or an active
    contextualized variant) — the **working span** a disambiguation-context
    embedding is recomputed over on demand. ``input_ref`` is canonical JSON: for raw
    input, ``{"chunk_id": ...}``; for contextualized input,
    ``{"chunk_id": ..., "context_version": ..., "run_id": ...}`` where ``run_id`` is
    the contextualization run the variant was read from — a provenance-only local
    locator, like :attr:`ContextualizedChunk.summary_run_id`. The persistence layer
    writes this JSON; this model only stores the ``Text`` column.

    Two per-anchor-class partial unique indexes back within-run idempotency, rather
    than one index over the nullable source span: SQL ``UNIQUE`` treats ``NULL`` as
    distinct from every other ``NULL``, so a single index spanning
    ``source_span_start`` / ``source_span_end`` would never deduplicate
    revision-anchored rows (whose spans are always ``NULL``).
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

    mention_id: str = Field(primary_key=True, nullable=False)
    run_id: int = Field(nullable=False)
    chunk_id: str = Field(sa_column=Column(ForeignKey("graph_chunks.chunk_id"), nullable=False))
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
    """A flat link recording one unordered intra-chunk mention pair within a run.

    The pair key is schema-enforced: composite primary key
    ``(run_id, mention_id_lo, mention_id_hi)``, a ``CHECK`` constraint enforcing
    ``mention_id_lo < mention_id_hi`` (canonical order, which also excludes
    self-pairs), and both endpoints foreign-keying into :attr:`Mention.mention_id`.
    Storing the pair once (not two directed rows) avoids double-counting while still
    serving symmetric lookups: querying either endpoint's column reaches the pair,
    and :attr:`mention_id_hi` carries its own index so a lookup rooted at the
    higher-ordered endpoint is indexed as directly as one rooted at the lower
    (covered by the primary key's leading columns).

    ``chunk_id`` is the chunk both endpoints share — a plain column, since the
    schema-enforced part of "same chunk" is that persistence validates it before
    insert, not a table-level constraint tying it to the endpoints' own
    ``chunk_id``s.
    """

    __tablename__ = "graph_mention_cooccurrences"
    __table_args__ = (
        CheckConstraint("mention_id_lo < mention_id_hi", name="ck_graph_mention_cooccurrences_ordered"),
        Index("ix_graph_mention_cooccurrences_chunk_id", "chunk_id"),
        Index("ix_graph_mention_cooccurrences_hi", "mention_id_hi"),
    )

    run_id: int = Field(primary_key=True, nullable=False)
    mention_id_lo: str = Field(
        sa_column=Column(ForeignKey("graph_mentions.mention_id"), primary_key=True, nullable=False)
    )
    mention_id_hi: str = Field(
        sa_column=Column(ForeignKey("graph_mentions.mention_id"), primary_key=True, nullable=False)
    )
    chunk_id: str = Field(nullable=False)
