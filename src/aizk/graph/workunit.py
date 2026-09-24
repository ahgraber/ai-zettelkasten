"""The per-document graph unit-of-work and its two enqueue entry points.

This module is the single write path the graph stage runs for one converted
document — ``split → persist_chunks → summarize → contextualize`` — plus the two
enqueue patterns that feed it. Chunk persistence and contextualization are **one
required pipeline**, not separately-gated steps: :func:`process_document` always
runs the whole path, and the contextualization toggle is an enqueue/eval lever
(which documents to enqueue, and the downstream raw-vs-contextualized comparison
via :func:`aizk.graph.variants.resolve_chunk_text`), never a per-unit
branch.

Both run modes are **enqueue patterns over one write path**, so the produced run
records are identical regardless of mode:

- :func:`enqueue_document` — incremental: enqueue one work-unit when a document
  is ingested.
- :func:`enqueue_backfill` — bulk: enqueue work-units for many documents
  (throttling and batching are the caller's scheduling concern).

Both dedupe on ``idempotency_key``, so re-enqueueing the same document reuses the
open work-unit rather than creating a second.

:func:`process_document` resolves its inputs by **locator** (``conversion_output_id``
→ Markdown, via the injected :class:`MarkdownSource`) but derives every run's
reuse/supersession by **content** — the durable ``source_id``, the markdown hash,
the version stamps, the prompt/model derivation keys, and the ordered chunk
content keys — never from ``id``, ``conversion_output_id``, the surrogate
``chunk_id``, or any other local row handle. The Markdown source is injected so the graph stage stays decoupled
from the conversion stage's blob storage and is deterministically testable.

**Two phases, one short write lock.** The Markdown fetch (S3) and the LLM passes
(summary + per-chunk revisions) run with **no write transaction held**; only the
persistence runs inside a single short ``BEGIN IMMEDIATE``, so model/IO latency
never blocks other writers (conversion, enqueue, retry/cancel) on the single
serialized SQLite writer.

**Monotonic currentness.** A read-only **preflight** checks the injected
:class:`OutputFreshness` before the fetch/generate work, and the same gate runs
again — authoritatively and atomically with the supersede — inside the write
transaction: if a newer ``ConversionOutput`` already exists for the source (or
the output belongs to a different source), the older unit writes nothing and
returns :class:`SkippedSuperseded`. An older work-unit can never supersede a
newer conversion's runs back to stale text, even if it runs late, and
foreign/superseded content never reaches the model.
"""

from aizk.contextualization.core.intake import (
    enqueue_backfill,
    enqueue_document,
    enqueued_outputs,
)
from aizk.contextualization.processing.document import (
    Cancelled,
    LoadedMarkdown,
    MarkdownSource,
    OutputFreshness,
    ProcessResult,
    SkippedSuperseded,
    process_document,
)
from aizk.contextualization.stage import (
    CONTEXTUALIZATION_DEFINITION,
    ContextualizationParams,
)

__all__ = [
    "CONTEXTUALIZATION_DEFINITION",
    "Cancelled",
    "ContextualizationParams",
    "LoadedMarkdown",
    "MarkdownSource",
    "OutputFreshness",
    "ProcessResult",
    "SkippedSuperseded",
    "enqueue_backfill",
    "enqueue_document",
    "enqueued_outputs",
    "process_document",
]
