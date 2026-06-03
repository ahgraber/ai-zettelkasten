# Proposal: Chunk Persistence and Contextualization

## Intent

The chunking capability today is a pure splitter: it emits structural chunks but persists nothing.
Every downstream stage — entity extraction, embedding, retrieval — needs chunks that are durably stored and replayable, not recomputed in-process and discarded.
Beyond raw chunks, the graph stage needs each chunk **made self-contained** before mention extraction can read it reliably: a per-document summary and a per-chunk contextualized variant that rewrites the chunk to resolve, inline, the references and definitions it depends on — without modifying the stored raw chunk.

This change adds the persistence and contextualization layer that sits between the existing splitter and the (separate, next) mention-extraction change:

- Persist raw chunks emitted by the splitter, idempotently and with full provenance.
- Produce and persist a per-document summary (one LLM pass per document).
- Produce and persist a per-chunk contextualized variant — the model's self-contained revision of the chunk — stored apart from the raw chunk, which is never modified and stays the cited, source-faithful unit.

Preserving each transformation stage — raw chunk, summary, contextualized chunk — is the cost of the repairability guarantee the graph architecture rests on: nothing upstream is discarded, so a later re-extraction, re-embedding, or re-clustering can replay from stored evidence.

Mention extraction (NER), co-occurrence, and the mention store are a **separate change** that consumes this change's output.

## Scope

Capabilities are listed in build-dependency order: `chunking` (persist the splitter's output) before `chunk-contextualization` (which reads persisted chunks to produce summaries and contextualized variants).

**In scope:**

- **`chunking` capability (delta — adds persistence).**

  - A persistence component that takes the splitter's emitted chunks and writes them to a durable store with full fidelity — **stable identity facts** (`chunk_id`, `content_hash`, `heading_path`, `ordinal`, `text`, `char_count`, source identity) on the content-addressed chunk, and **generation-varying facts** (`markdown_hash_xx64`, `splitter_version`, `span`) on the `chunk_run` and its manifest, so an emitted chunk round-trips by joining its identity to the generation that produced it.
  - Content-addressed, immutable chunk identities: persisting an existing `chunk_id` reuses the identity (no duplicate, no mutation); `chunk_id` stays run-independent so an unchanged chunk keeps its identity across re-chunks.
  - Run-level supersession without identity mutation: chunking runs are scoped by the **durable source identity (`aizk_uuid`)**; each chunk links to the run that produced it via an append-only `chunk_run_manifest` (recording the chunk's `span` in that generation's markdown), and each run records (in `chunk_run_input`) the `conversion_output_id` it consumed; re-chunking opens a new run for the source and transitions the prior to superseded, leaving all prior identities and manifests intact.
    A `chunk_id` is current iff it is in the active run's manifest.
  - The existing splitter contract is unchanged: the splitter remains a pure, I/O-free function; persistence is a distinct component that calls it.

- **`chunk-contextualization` capability (new).**

  - A per-document **summary** produced by one LLM pass over the document, persisted with a `summary_version` and the document provenance needed to regenerate it.
  - A per-chunk **contextualized variant** produced from a grounded summary + 2-prior/1-next neighbor framing: the model's **self-contained revision** of the chunk (references resolved inline), keyed by `chunk_id` and a `context_version`.
    An empty revision means the chunk was already self-contained.
  - The original chunk text is never modified; the contextualized variant is a separate, derived artifact and separately addressable.
  - Contextualization runs as a **worker-driven stage mirroring the conversion stage**: each document to contextualize is a status-bearing work-unit, claimed and leased through the shared pipeline runtime, with retry/cancel/timeout and an operator view.
  - The stage is invocable in both a **bulk/backfill** mode (enqueue many documents, batched writes) and an **incremental** mode (enqueue one document on ingest); both enqueue work-units processed by the single worker write path, so the persisted output is identical regardless of mode.

**Out of scope (named, separate changes):**

- **Mention extraction + mention store** — NER over contextualized chunks, intra-chunk co-occurrence, and the append-only lexical mention store.
  The next change (`mention-extraction-foundation`), which consumes this change's contextualized chunks.
- **Embeddings of any kind** — chunk embeddings (ADR-007) and mention context embeddings (recomputed on demand at canonicalization resolve time, never stored).
- **Entity canonicalization** — entity store, lineage, create-vs-assign.
- **Graph edges / retrieval / temporal model.**
- **Compaction / retention sweep** — this change records the supersession marker (the staleness signal) but does not purge anything.
  The sweep that acts on that signal — compacting superseded chunks and contextualized revisions, and later invalidated artifacts and retired lineage — is deferred to an explicitly named future change, `artifact-compaction-retention`, triggered when canonicalization introduces lineage churn or when storage growth tracks revision count rather than corpus size.
  Recording the marker here keeps that future sweep actionable instead of forcing it to reconstruct what is stale.

## Approach

> Mechanism sandbox; contracts live in the delta specs, chosen mechanisms formalize in `design.md`.

The graph stage gets its own package (`src/aizk/graph/`, mirroring `src/aizk/chunking/`).
The pipeline reads conversion Markdown artifacts, runs the existing `aizk.chunking` splitter in-process, persists the resulting chunks, then summarizes each document and contextualizes each chunk.

- **Persistence** uses the project SQLite stack (SQLModel + Alembic).
  Chunk identities are keyed by content-addressed `chunk_id` and carry stable facts only; generation-varying facts and each chunk's `span` live on an append-only `chunk_run_manifest`, and the consumed `conversion_output_id` on a `chunk_run_input` row.
  All runs are scoped by the source `aizk_uuid`.
  Summary and contextualized-revision rows are run-scoped, with their runs carrying derivation keys (markdown hash + prompt/model profile for the summary; summary + ordered chunk identities + `splitter_version` + 2p/1n policy + prompt/model profile for the variant) alongside `summary_version` / `context_version`; the variant also records `summary_run_id` + `chunking_run_id` as locator provenance.
  Migration-tree placement (shared conversion database vs. a dedicated graph database) is a design decision.
- **Summary + contextualization** use the project LLM stack (`pydantic-ai-slim`).
  Summary is one pass per document; contextualization is one pass per chunk with two prior chunks and one following chunk as local context.
  This is the LLM cost driver; batching, content-hash caching, and model-tier selection are design concerns.
- **Contextualized chunk as a revision** (not an additive delta): persist the model's self-contained rewrite of the chunk and consume it directly at the point of use.
  A delta cannot express dereferencing — resolving a reference inline rewrites the text — so the variant stores the revised text; an empty revision means the chunk was already self-contained.
  The raw chunk is never modified and retrieval still cites it.
- **DB-ops profile.**
  Chunking emits ~10 rows/document; this change adds a summary row per document and a contextualized-revision row per chunk — still chunk-order-of-magnitude, not the mention-order-of-magnitude that the next change introduces.
  Backfill batches inserts into a few transactions per document against the serialized SQLite writer; WAL keeps retrieval readers unblocked.

Because the LLM steps are non-deterministic, the testable contracts assert **structure, provenance, and guardrails** — every chunk has a persisted record; every document has a grounded summary tagged with `summary_version`; every chunk has a contextualized variant tagged with `context_version` and its source `chunk_id`; prompt/window/model identity is carried in derivation keys; revisions past the chunk-relative length budget are rejected — rather than exact output text.
Tests stub the model client.

## Decisions Carried Into Design

- **The splitter stays pure.**
  Persistence is a separate component; the splitter's I/O-free, deterministic contract is preserved unchanged.
- **Invalidation is run-level; rows are immutable.**
  Every stage emits a versioned run carrying a status (active or superseded) and a derivation key.
  Re-processing opens a new run and transitions the prior to superseded — the one blessed mutation — leaving rows untouched.
  This unified run/dataset-version model is the primitive the `pipeline-stage-runtime` refactor extracts.
- **Contextualized text is a derived revision, not a modification of the source.**
  The source-faithful chunk text is never modified; the contextualized variant is a separate, bounded, grounded rewrite that resolves references inline.
- **Worker-driven stage; two enqueue modes.**
  Contextualization is a worker-driven stage mirroring conversion — a status-bearing work-unit table claimed and leased through the shared runtime.
  Bulk backfill and incremental ingest are enqueue patterns over that table feeding one worker write path; mode affects enqueue volume and scheduling, not the persisted output.

## Schema Impact

**OpenAPI (`conversion-api`):** No changes.
This change introduces no HTTP endpoints; chunk persistence and contextualization are internal pipeline stages with no API surface.
The `before/` snapshot equals the committed baseline `.specs/schemas/conversion-api-openapi.json`, and that snapshot is the expected post-change snapshot — no `schemas/expected.md` is generated.

**Database (SQLite):** Adds new tables and an Alembic migration — a content-addressed chunk table (stable facts only), an append-only `chunk_run_manifest` (carrying each chunk's `span`), a `chunk_run_input` row (the consumed `conversion_output_id`), a run-scoped per-document summary table, a run-scoped contextualized-chunk revision table, and a status-bearing graph work-unit table (mirroring `conversion_jobs`).
The stage-run and transition-event tables are reused from `aizk.pipeline`, not added here.
These are **not** tracked by `.specs/.sdd/schema-config.yaml` (which extracts only the conversion-api OpenAPI), so no DB snapshot is captured here; the `schema-migrations` capability's ORM-vs-migration equivalence test covers structural fidelity instead.
Concrete table shapes and migration-tree placement are decided in `design.md`.

## Open Questions

- **Database boundary.**
  Does the graph stage share the conversion service's SQLite database and migration tree (`src/aizk/conversion/migrations/`) or get its own?
  Affects migration placement and the `schema-migrations` capability.
  Resolved in `design.md`.
- **Chunk ↔ conversion-output linkage.**
  How do chunks and runs relate to the source and its converted outputs?
  Resolved in `design.md`: all runs are scoped by the durable source identity (`aizk_uuid`), so re-conversion of the same source supersedes within one scope rather than forking a parallel current generation; the consumed `conversion_output_id` is recorded as a retrieval locator (never the scope, never a derivation input).
- **Contextualization cost vs. a cheaper first pass.**
  Per-chunk contextualization is the LLM cost driver.
  Recommendation: make contextualization a toggleable stage so a downstream raw-vs-contextualized extraction-quality comparison is possible, and so the first backfill can be a bounded corpus sample.
- **Summary granularity.**
  One summary per document is assumed.
  For very long documents, is a single summary sufficient, or is a hierarchical summary needed?
  Recommendation: single summary now; revisit if long-document extraction quality suffers.
