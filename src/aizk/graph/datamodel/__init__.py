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

from aizk.graph.datamodel.chunks import Chunk, ChunkRunApplicability, ChunkRunInput, ChunkRunManifest
from aizk.graph.datamodel.jobs import ContextualizationJob, ExtractionJob
from aizk.graph.datamodel.memo import MEMO_KIND_REVISION, MEMO_KIND_SUMMARY, ContextualizationOutputMemo, MemoKind
from aizk.graph.datamodel.mentions import (
    ANCHOR_KIND_REVISION,
    ANCHOR_KIND_SOURCE,
    INPUT_KIND_CONTEXTUALIZED,
    INPUT_KIND_RAW,
    AnchorKind,
    ExtractionRunApplicability,
    ExtractionRunInput,
    InputKind,
    Mention,
    MentionCooccurrence,
)
from aizk.graph.datamodel.summaries import DocumentSummary, SummaryRunApplicability
from aizk.graph.datamodel.variants import (
    ContextualizationRunApplicability,
    ContextualizationRunInput,
    ContextualizedChunk,
)

__all__ = [
    "ANCHOR_KIND_REVISION",
    "ANCHOR_KIND_SOURCE",
    "AnchorKind",
    "Chunk",
    "ChunkRunApplicability",
    "ChunkRunInput",
    "ChunkRunManifest",
    "ContextualizationJob",
    "ContextualizationOutputMemo",
    "ContextualizationRunApplicability",
    "ContextualizationRunInput",
    "ContextualizedChunk",
    "DocumentSummary",
    "ExtractionJob",
    "ExtractionRunApplicability",
    "ExtractionRunInput",
    "INPUT_KIND_CONTEXTUALIZED",
    "INPUT_KIND_RAW",
    "InputKind",
    "MEMO_KIND_REVISION",
    "MEMO_KIND_SUMMARY",
    "MemoKind",
    "Mention",
    "MentionCooccurrence",
    "SummaryRunApplicability",
]
