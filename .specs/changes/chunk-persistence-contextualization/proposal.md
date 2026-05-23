# Proposal: Chunk Persistence and Contextualization

## Intent

The chunking capability today is a pure splitter: it emits structural chunks but persists nothing.
Every downstream stage — entity extraction, embedding, retrieval — needs chunks that are durably stored and replayable, not recomputed in-process and discarded.
Beyond raw chunks, the graph stage needs each chunk made **self-contained and reference-resolved** before mention extraction can read it reliably: a per-document summary and a per-chunk contextualized variant with coreferences resolved.

This change adds the persistence and contextualization layer that sits between the existing splitter and the (separate, next) mention-extraction change:

- Persist raw chunks emitted by the splitter, idempotently and with full provenance.
- Produce and persist a per-document summary (one LLM pass per document).
- Produce and persist a per-chunk contextualized variant, stored as a delta (the added context only) so the corpus is not copied twice and the source-faithful chunk text is never rewritten.

Preserving each transformation stage — raw chunk, summary, contextualized chunk — is the cost of the repairability guarantee the graph architecture rests on: nothing upstream is discarded, so a later re-extraction, re-embedding, or re-clustering can replay from stored evidence.

Mention extraction (NER), co-occurrence, and the mention store are a **separate change** that consumes this change's output.

## Scope

Capabilities are listed in build-dependency order: `chunking` (persist the splitter's output) before `chunk-contextualization` (which reads persisted chunks to produce summaries and contextualized variants).

**In scope:**

- **`chunking` capability (delta — adds persistence).**

  - A persistence component that takes the splitter's emitted chunks and writes them to a durable store, preserving every chunk field and provenance value (`chunk_id`, `content_hash`, `heading_path`, `ordinal`, `text`, `char_count`, `converted_artifact_id`, `markdown_hash_xx64`, `span`, `splitter_version`).
  - Content-addressed, immutable chunk rows: persisting an existing `chunk_id` reuses the row (no duplicate, no mutation); `chunk_id` stays run-independent so an unchanged chunk keeps its identity across re-chunks.
  - Run-level supersession without row mutation: chunks belong to a chunking run via append-only membership; re-chunking opens a new run and transitions the prior run to superseded, leaving all prior rows and memberships intact.
    A `chunk_id` is current iff it is a member of the active run.
  - The existing splitter contract is unchanged: the splitter remains a pure, I/O-free function; persistence is a distinct component that calls it.

- **`chunk-contextualization` capability (new).**

  - A per-document **summary** produced by one LLM pass over the document, persisted with a `summary_version` and the document provenance needed to regenerate it.
  - A per-chunk **contextualized variant** produced from a `<summary><prior_chunk><working_chunk><next_chunk>` framing, with coreferences in the working chunk resolved to explicit referents, stored as a **delta** (the added context text, not a full re-copy of the chunk) keyed by `chunk_id` and a `context_version`.
  - The original chunk text is never modified; the contextualized variant is additive and separately addressable.
  - The pipeline is invocable in both a **bulk/backfill** mode (process many documents, batched writes) and an **incremental** mode (process one document on ingest); the persisted output is identical regardless of mode.

**Out of scope (named, separate changes):**

- **Mention extraction + mention store** — NER over contextualized chunks, intra-chunk co-occurrence, and the append-only lexical mention store.
  The next change (`mention-extraction-foundation`), which consumes this change's contextualized chunks.
- **Embeddings of any kind** — chunk embeddings (ADR-007) and mention context embeddings (recomputed on demand at canonicalization resolve time, never stored).
- **Entity canonicalization** — entity store, lineage, create-vs-assign.
- **Graph edges / retrieval / temporal model.**
- **Compaction / retention sweep** — this change records the supersession marker (the staleness signal) but does not purge anything.
  The sweep that acts on that signal — compacting superseded chunks and contextualized deltas, and later invalidated artifacts and retired lineage — is deferred to an explicitly named future change, `artifact-compaction-retention`, triggered when canonicalization introduces lineage churn or when storage growth tracks revision count rather than corpus size.
  Recording the marker here keeps that future sweep actionable instead of forcing it to reconstruct what is stale.

## Approach

> Mechanism sandbox; contracts live in the delta specs, chosen mechanisms formalize in `design.md`.

The graph stage gets its own package (`src/aizk/graph/`, mirroring `src/aizk/chunking/`).
The pipeline reads conversion Markdown artifacts, runs the existing `aizk.chunking` splitter in-process, persists the resulting chunks, then summarizes each document and contextualizes each chunk.

- **Persistence** uses the project SQLite stack (SQLModel + Alembic).
  Chunk rows are keyed by content-addressed `chunk_id` and linked to a chunking run by append-only membership; summary and contextualized-delta rows are run-scoped and carry an input fingerprint (markdown hash for the summary; summary + neighbor identities for the variant) alongside `summary_version` / `context_version`.
  Migration-tree placement (shared conversion database vs. a dedicated graph database) is a design decision.
- **Summary + contextualization** use the project LLM stack (`pydantic-ai-slim`).
  Summary is one pass per document; contextualization is one pass per chunk with neighbor framing.
  This is the LLM cost driver; batching, content-hash caching, and model-tier selection are design concerns.
- **Contextualized chunk as a delta** (not a copy): persist only the added context blurb and reconstruct the embedded text (`summary` + neighbors + working chunk) at the point of use, so the corpus is not stored twice and the two representations cannot drift.
  Retrieval still cites the raw chunk.
- **DB-ops profile.**
  Chunking emits ~10 rows/document; this change adds a summary row per document and a contextualized-delta row per chunk — still chunk-order-of-magnitude, not the mention-order-of-magnitude that the next change introduces.
  Backfill batches inserts into a few transactions per document against the serialized SQLite writer; WAL keeps retrieval readers unblocked.

Because the LLM steps are non-deterministic, the testable contracts assert **structure and provenance** — every chunk has a persisted record; every document has a summary tagged with `summary_version`; every chunk has a contextualized variant tagged with `context_version` and its source `chunk_id`; coreference spans in the working chunk are resolved — rather than exact output text.
Tests stub the model client.

## Decisions Carried Into Design

- **The splitter stays pure.**
  Persistence is a separate component; the splitter's I/O-free, deterministic contract is preserved unchanged.
- **Invalidation is run-level; rows are immutable.**
  Every stage emits a versioned run carrying a status (active or superseded) and an input fingerprint.
  Re-processing opens a new run and transitions the prior to superseded — the one blessed mutation — leaving rows untouched.
  This unified run/dataset-version model is the primitive the `pipeline-stage-runtime` refactor extracts.
- **Contextualized text is a derived delta, not a rewrite.**
  The source-faithful chunk text is never modified; the contextualized variant is additive and reconstructable.
- **One pipeline, two run modes.**
  Bulk backfill and incremental ingest share the same persistence contract; mode affects batching and orchestration, not output.

## Schema Impact

**OpenAPI (`conversion-api`):** No changes.
This change introduces no HTTP endpoints; chunk persistence and contextualization are internal pipeline stages with no API surface.
The `before/` snapshot equals the committed baseline `.specs/schemas/conversion-api-openapi.json`, and that snapshot is the expected post-change snapshot — no `schemas/expected.md` is generated.

**Database (SQLite):** Adds new tables and an Alembic migration — a content-addressed chunk table, a stage-run table plus an append-only chunk-run membership table, a run-scoped per-document summary table, and a run-scoped contextualized-chunk delta table.
These are **not** tracked by `.specs/.sdd/schema-config.yaml` (which extracts only the conversion-api OpenAPI), so no DB snapshot is captured here; the `schema-migrations` capability's ORM-vs-migration equivalence test covers structural fidelity instead.
Concrete table shapes and migration-tree placement are decided in `design.md`.

## Open Questions

- **Database boundary.**
  Does the graph stage share the conversion service's SQLite database and migration tree (`src/aizk/conversion/migrations/`) or get its own?
  Affects migration placement and the `schema-migrations` capability.
  Resolved in `design.md`.
- **Chunk ↔ conversion-output linkage.**
  Chunks reference `converted_artifact_id`; is the chunk table a foreign key into the conversion outputs table, or does it store the identifier opaquely?
  Recommendation: opaque now, FK when the boundary is settled in design.
- **Contextualization cost vs. a cheaper first pass.**
  Per-chunk contextualization is the LLM cost driver.
  Recommendation: make contextualization a toggleable stage so a downstream raw-vs-contextualized extraction-quality comparison is possible, and so the first backfill can be a bounded corpus sample.
- **Summary granularity.**
  One summary per document is assumed.
  For very long documents, is a single summary sufficient, or is a hierarchical summary needed?
  Recommendation: single summary now; revisit if long-document extraction quality suffers.
