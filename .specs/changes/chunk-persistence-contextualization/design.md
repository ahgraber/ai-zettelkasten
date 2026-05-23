# Design: Chunk Persistence and Contextualization

## Context

The chunking capability today is the pure splitter in `src/aizk/chunking/` (`split()`, `Chunk`, `SPLITTER_VERSION`): deterministic, I/O-free, persisting nothing.
This change adds the persistence and contextualization layer that turns splitter output into a durable, replayable substrate the mention-extraction change consumes.

Constraints shaping the design:

- **Database (ADR-003).**
  SQLite + `sqlite-vec` via SQLModel/SQLAlchemy, Alembic migrations, WAL + `synchronous=NORMAL`, a **single serialized writer**, replicated by Litestream (which assumes one writer process).
  No vectors are introduced by this change.
- **Repairability (ADR-006 Infrastructure Impact).**
  Every transformation stage — raw chunk, document summary, contextualized chunk — is preserved; nothing upstream is discarded, and re-processing supersedes prior artifacts rather than overwriting them.
  The staleness signal recorded here feeds the deferred `artifact-compaction-retention` sweep.
- **Pipeline pattern (ADR-005, conversion precedent).**
  ADR-005 states the chunking pipeline mirrors the conversion stage's event-sourced state transitions.
  The conversion realization (`conversion-job-event-log`) is lightweight: an append-only event table written in the **same transaction** as a **mutable authoritative current-state row** (`ConversionJob.status`) via a single `record_transition` helper.
  The run model below applies that same shape — the only blessed mutation is a status transition.
- **Prerequisite refactor (`pipeline-stage-runtime`).**
  Contextualization runs as a worker-driven stage with an operator UI.
  That worker harness, transition log, adapter composition, UI scaffold, **and the stage-run/dataset-version primitive defined below** are extracted into a shared runtime by the `pipeline-stage-runtime` change, implemented before this one (build order: `pipeline-stage-runtime` → this change).
- **Model provider (ADR-004).**
  Summary and contextualization use **`pydantic-ai`** as the LLM library, behind a thin injected client; output is non-deterministic, so tests stub that client.

Capabilities build in dependency order: `chunking` (persist splitter output) before `chunk-contextualization` (reads persisted chunks).

## Decisions

### Decision: GraphStagePackageAndSharedDatabase

**Chosen:** A new `src/aizk/graph/` package houses the persistence and contextualization pipeline; it imports and calls the existing `aizk.chunking.split()` rather than reimplementing it.
Graph-stage tables are added to the **existing conversion SQLite database and Alembic migration tree**, not a separate database.

**Rationale:** One SQLite file means one Litestream replication target and one serialized writer to reason about (ADR-003).
Downstream stages (mentions → chunks) need cross-references that are foreign keys within one database rather than cross-database joins.
The graph schema is ordinary relational data and stays portable to Postgres on the path ADR-003 documents.

**Alternatives considered:**

- **Dedicated graph database/file.**
  A second Litestream target and writer, and it forecloses the chunk↔mention foreign key the next change wants.
  Not justified at MVP scale.
- **Reimplement splitting inside the graph package.**
  Duplicates the splitter contract and its `splitter_version` discipline.
  Rejected — the splitter is reused as-is.

### Decision: UnifiedStageRunDatasetVersion

**Chosen:** Every stage that produces derived artifacts records a **run** (`run_id`, stage, the version stamps and `input_fingerprint` that produced it, `supersedes_run_id`, `status ∈ {active, superseded}`).
At most one run per stage per scope (document) is `active`.
Run-produced rows are **immutable**; **invalidation is run-level** — superseding a prior run is expressed only as a `status` transition `active → superseded`, co-committed with a transition event.
Compaction of superseded runs is a separate, retention-policy operation (the deferred `artifact-compaction-retention` change), not a mutation.
This run/dataset-version concept is the shared primitive the `pipeline-stage-runtime` refactor extracts; this change is its first consumer (chunking and contextualization runs).

**Rationale:** It resolves the "never mutate vs. mark superseded" tension cleanly: content rows are never edited, and the single blessed mutation — a run status transition + append-only event — is exactly the conversion stage's house pattern (mutable current-state status + append-only event log).
It gives one coherent invalidation story across stages ("one active dataset per stage at a time"), so a downstream consumer reads the active run and ignores superseded ones.
Keeping invalidation at the run level avoids per-row lifecycle bookkeeping and avoids forcing consumers to reason about per-observation history.

**Alternatives considered:**

- **Per-row `superseded_at`/`superseded_by` column.**
  Marking a row superseded is itself an in-place mutation, contradicting immutability; and it scatters lifecycle state across rows instead of one run record.
- **Strictly immutable rows with no status anywhere.**
  Then "which dataset is current?"
  is unanswerable without re-deriving; the run status is the minimal authoritative current-state pointer.

### Decision: ChunkStoreContentAddressedRowsWithRunMembership

**Chosen:** A `chunk` table keyed by the content-addressed `chunk_id`, carrying every `Chunk` field; chunk content rows are immutable and **run-independent** (an unchanged chunk keeps the same `chunk_id` across re-chunks).
A chunking run (per `converted_artifact_id` + `splitter_version`) and an append-only `chunk_run_membership` table link chunks to the run that emitted them.
A `chunk_id` is current iff it is a member of the document's active chunking run.
Persisting an existing `chunk_id` reuses the row; re-chunking creates a new run and new memberships without touching prior rows or memberships.

**Rationale:** `chunk_id` already encodes address + content (chunking spec), so it must stay run-independent for the ADR-005 churn signal ("a `chunk_id` that persists across re-chunk is unchanged; one that disappears is stale") and for stable downstream references (a mention's `chunk_id` FK, on-demand embedding windows).
Run membership turns "disappeared `chunk_id`" into "not a member of the active run" — derivable with no row mutation, and unchanged chunks are naturally shared across runs via multiple membership rows.

**Alternatives considered:**

- **Run-scoped `chunk_id` (hash includes `run_id`).**
  Breaks content-addressing and the churn signal; an unchanged section would change identity every re-chunk.
  Rejected.
- **Per-row supersession marker (no runs).**
  The mutation problem above; also can't represent "shared unchanged chunk" without ambiguity.

### Decision: ContextualizationRunScopedRecordsAsDeltaWithInputFingerprints

**Chosen:** A contextualization run produces a `document_summary` and one `contextualized_chunk` per chunk.
Because contextualization output depends on model + inputs (not just source content), these records are **run-scoped** and carry an `input_fingerprint`: the summary's fingerprint includes the source markdown hash + `summary_version`; the variant's includes the summary identity + neighboring chunk identities + `context_version`.
The variant stores **only the added context blurb** (a delta), and the consumed text is reconstructed at use time from `summary + neighbor chunks + working chunk` at the recorded inputs.
A change to any fingerprinted input (new markdown, changed neighbor, version bump) yields a new run that supersedes the prior; unchanged inputs + versions reuse the active run.

**Rationale:** Keying only on `(document, summary_version)` / `(chunk_id, context_version)` is insufficient — the same document id with new markdown, or the same working chunk with changed neighbors, must produce new output under an unchanged model/prompt version.
The `input_fingerprint` makes idempotency mean "unchanged inputs **and** version," which is the correct condition.
Delta storage avoids a second full copy of the corpus and keeps the raw chunk the single source-faithful text (the "source chunk never modified" contract); reconstruction is deterministic given the recorded inputs.

**Alternatives considered:**

- **Key only by `(document, summary_version)` / `(chunk_id, context_version)`.**
  Misses input changes under a stable version — stale output silently treated as current.
  Rejected (this was the original gap).
- **Store the full contextualized text.**
  Doubles corpus storage and risks drift; retrieval still cites the raw chunk, so the copy earns nothing.

### Decision: LLMContextualizationWithStubbableClientAndQualityEval

**Chosen:** Summary (one pass per document) and contextualization (one pass per chunk, framed `<instructions><summary><prior_chunk><working_chunk><next_chunk>`) call **`pydantic-ai`** (ADR-004 model provider) behind a thin injected client interface owned by the graph stage.
Tests inject a stub client, so the persistence, provenance, input-fingerprint, idempotency, supersession, and mode-independence contracts are exercised deterministically.
The _quality_ of self-containment / coreference resolution is measured by an offline evaluation against a gold set, not asserted on live model output (see Verification Waivers).
Contextualization is a **toggleable stage**, and the input actually used is recorded, enabling a raw-vs-contextualized comparison downstream.

**Rationale:** The deterministic contracts must be testable without a live model; an injected client makes that clean.
Self-containment is an output property of a non-deterministic model — its honest evidence is an eval distribution, not a unit assertion.
The toggle bounds first-backfill LLM cost and lets the next change measure whether contextualization improves extraction.

**Alternatives considered:**

- **Assert exact contextualized text in tests.**
  Brittle and false — model output is non-deterministic.
- **Always-on contextualization, no toggle.**
  Removes the cheap baseline and the cost lever.

### Decision: ContextualizationRunsAsAStageOnTheSharedRuntime

**Chosen:** Contextualization executes as a worker-driven stage on the shared `pipeline-stage-runtime`, with an operator UI view.
This change supplies the contextualization **stage adapter** (summarize a document, contextualize its chunks) and the chunk/summary/variant stores; the worker harness, transition log, adapter composition root, run primitive, and UI scaffold come from the prerequisite runtime change.

**Rationale:** Contextualization and (later) extraction need the same worker + operator UI machinery the conversion stage already has.
Building it once in the runtime and consuming it here avoids divergent copies of reliability-critical code.
This change owns only what is stage-specific.

**Build-order dependency:** `pipeline-stage-runtime` → this change.
The runtime is specced after this change but implemented before it; the delta specs here are mechanism-independent, so only `tasks.md`'s stage-adapter group depends on the runtime interface.
The runtime ships no generic UI, so this stage builds its own operator view.

**Source identity for runtime events.**
The runtime requires every work-unit and transition event to carry the `aizk_uuid` source identity.
This stage resolves it from the converted artifact: `converted_artifact_id → ConversionOutput.aizk_uuid`, carried onto the chunking/contextualization runs and their transition events so a source's progress is resolvable across stages.
The contextualization run's `scope_key` is the document (per-document summary + variants); the chunking run's `scope_key` is the converted artifact.

**Generic lifecycle mapping.**
A contextualization work-unit (summarize a document, contextualize its chunks) maps onto the runtime's generic lifecycle: `succeeded` on completion; `failed` classified `retryable` on transient model/IO errors and `permanent` on malformed input; `cancelled` and `timed_out` per the harness.
Contextualization runs in-process (no subprocess isolation), so the subprocess-specific termination guarantees do not apply.

**Alternatives considered:**

- **Build a contextualization-specific worker + UI here.**
  Triples reliability machinery across stages — the duplication the refactor exists to prevent.

### Decision: RunModeAffectsBatchingOnly

**Chosen:** The pipeline exposes a per-document processing unit.
Bulk/backfill mode runs it across many documents, batching inserts into a few transactions per document (throttled background work); incremental mode runs it for one document on ingest.
Both call the identical write path, so the active run's record set and provenance are identical; only batching and scheduling differ.

**Rationale:** One write path makes the run-mode-independence contract hold by construction.
Batching per document keeps backfill inside ADR-003's serialized-writer budget (~100 sustained writes/sec is its migration trigger); WAL keeps readers unblocked.

**Alternatives considered:**

- **Separate bulk and incremental write paths.**
  Invites the divergence the contract forbids.
- **Row-at-a-time inserts during backfill.**
  Hammers the serialized writer.

## Architecture

```text
conversion Markdown artifact
        │
        ▼
  aizk.chunking.split()  ── pure, reused, unchanged ──►  [Chunk, …]
        │
        ▼
  graph write path  (one path; bulk batches, incremental per-doc)
        │
        ├── chunking run (per artifact+splitter_version, status active|superseded)
        │      └── chunk rows (content-addressed, immutable) ⇄ chunk_run_membership (append-only)
        │
        ▼
  LLM (pydantic-ai, stubbable)
        └── contextualization run (input_fingerprint: markdown hash / neighbors+summary, status)
               ├── document_summary  (run-scoped)
               └── contextualized_chunk  (run-scoped, delta blurb only)
        │
        ▼
  transition event log  (append-only, same transaction; run status transitions live here)

invalidation = run.status active→superseded (the one blessed mutation); rows immutable.
reconstruct-at-use:  summary + neighbor chunks + working chunk  ──►  contextualized text
                     (raw chunk text never modified)
```

All graph tables live in the conversion SQLite database; one serialized writer; Litestream replicates the WAL.

## Risks

- **LLM cost of per-chunk contextualization.**
  Mitigation: toggleable stage, content-fingerprint caching, a bounded corpus sample for the first backfill.
- **Self-containment quality is model-dependent, not guaranteed.**
  Mitigation: structural contracts tested via stub; quality measured by offline eval against a gold set (Verification Waivers); the raw-vs-contextualized toggle quantifies the benefit.
- **Backfill write throughput against the serialized writer (ADR-003).**
  Mitigation: batch inserts per document, throttle backfill, keep reads on WAL.
- **Superseded-run accumulation.**
  Re-chunking and version bumps grow storage with revision count.
  Mitigation: invalidation is a cheap run-status transition; the bulk content of superseded runs is reclaimed by the `artifact-compaction-retention` change (deletion by retention policy is not a mutation and does not violate row immutability).
- **Shared-database coupling.**
  Adding graph tables to the conversion migration tree couples the stages' migrations.
  Mitigation: ordinary relational data on ADR-003's documented Postgres path; a migration-ordering concern, not a data-model one.
- **Delta-reconstruction stability.**
  Mitigation: reconstruct strictly from the recorded summary, neighbor chunks, and working chunk at the run's recorded inputs — deterministic given those inputs.

## Verification Waivers

- **Requirement:** The contextualized variant is self-contained (`chunk-contextualization`).
  **Reason:** Self-containment / coreference resolution is a property of non-deterministic LLM output; there is no deterministic automated execution that proves the model resolves references on arbitrary corpus text.
  The _persistence, provenance, and input-fingerprint_ of the variant are tested deterministically with a stubbed client; the _resolution quality_ is the waived dimension.
  **Manual evidence:** Offline contextualization-quality evaluation against the graph-stage gold set (established with the canonicalization validation-gate work); recorded as an eval report under `data/graph_research/` when the gold set lands.
  **Recorded:** 2026-05-22
