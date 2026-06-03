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

**Chosen:** Every stage that produces derived artifacts records a **run** (`run_id`, stage, the version stamps and `derivation_key` that produced it, `supersedes_run_id`, `status ∈ {active, superseded}`).
At most one run per stage per scope is `active`; the scope is the **durable source identity (`aizk_uuid`)**, never a local artifact row id (`ConversionOutput.id`), so re-conversion of the same source supersedes within one scope rather than forking a parallel one.
Run-produced rows are **immutable**; **invalidation is run-level** — superseding a prior run is expressed only as a `status` transition `active → superseded` on the run pointer, with run lineage carried by the append-only `supersedes_run_id` chain.
The run-pointer flip itself is not evented; work-unit lifecycle transitions are logged separately in `pipeline_events` by the runner-driven stage adapter, each carrying the `run_id` and `aizk_uuid` (matching `aizk.pipeline.run` / `aizk.pipeline.events`).
Compaction of superseded runs is a separate, retention-policy operation (the deferred `artifact-compaction-retention` change), not a mutation.
This run/dataset-version concept is the shared primitive the `pipeline-stage-runtime` refactor extracts; this change is its first consumer (chunking and contextualization runs).

Identity vocabulary follows the conversion pattern: local `id` values are row handles, not derivation inputs; a work-unit `idempotency_key` deduplicates enqueue requests; a run or derived row `derivation_key` decides whether model/config-dependent output is reused or superseded; and durable source identity comes from `aizk_uuid` plus content hashes, version stamps, prompt hashes, model profile, and ordered source ids.

**Rationale:** It resolves the "never mutate vs. mark superseded" tension cleanly: content rows are never edited, and the single blessed mutation — the run-pointer `status` transition — mirrors the conversion stage's mutable current-state-status pattern.
The append-only history lives in the `supersedes_run_id` run chain (run lineage) and, for work-unit lifecycle, in the runner's `pipeline_events` log; the run-pointer flip itself carries no event.
It gives one coherent invalidation story across stages ("one active dataset per stage at a time"), so a downstream consumer reads the active run and ignores superseded ones.
Keeping invalidation at the run level avoids per-row lifecycle bookkeeping and avoids forcing consumers to reason about per-observation history.

**Alternatives considered:**

- **Per-row `superseded_at`/`superseded_by` column.**
  Marking a row superseded is itself an in-place mutation, contradicting immutability; and it scatters lifecycle state across rows instead of one run record.
- **Strictly immutable rows with no status anywhere.**
  Then "which dataset is current?"
  is unanswerable without re-deriving; the run status is the minimal authoritative current-state pointer.

### Decision: ChunkStoreStableIdentityWithRunEmissionEdge

**Chosen:** The chunking stage's facts are split by what they are _about_, because four jobs are genuinely distinct: `aizk_uuid` answers "which source history?"; `markdown_hash_xx64 + splitter_version` answers "which chunking generation?"; `chunk_id` answers "which stable chunk identity?"; and the manifest answers "which chunks did this generation produce?".

- **`chunk`** — a table keyed by the content-addressed `chunk_id`, carrying **only stable identity facts**: `chunk_id`, the source/`doc_id` (`= str(aizk_uuid)`), `text`, `content_hash`, `char_count`, `heading_path`, `ordinal`.
  Rows are immutable, **run-independent**, and deduped: an unchanged chunk keeps the same `chunk_id` and the same single row across re-chunks.
  No generation-varying fact lives here.
- **`chunk_run`** — a generation marker (a `PipelineRun`, stage `chunking`, `scope_key = str(aizk_uuid)`, `derivation_key = {markdown_hash_xx64, splitter_version}`, `status`, `supersedes_run_id`).
- **`chunk_run_input`** — the run's **input** (mirroring `ConversionInput`): one row per run recording the consumed `conversion_output_id` as a retrieval locator (a small typed graph provenance row keyed by `run_id`, since the shared `PipelineRun` carries no graph-specific columns) alongside the `markdown_hash_xx64` that verifies it.
- **`chunk_run_manifest`** — the run's **manifest** of produced chunks (mirroring conversion's manifest of produced artifacts): an append-only row per `(chunk_run_id, chunk_id, span)` recording _which chunks the run produced and where each sat in that markdown_.

A `chunk_id` is current iff it is in the **active** `chunk_run`'s manifest.
Persisting an existing `chunk_id` reuses the row; re-chunking opens a new `chunk_run` (superseding the prior for that `aizk_uuid`) and writes a fresh manifest, never touching prior rows or manifests.

**Rationale:** A `chunk` row is content-addressed and shared across every generation that re-emits it, so **every field on it must be invariant for that `chunk_id`** — true for `text`/`content_hash`/`heading_path`/`ordinal` (they are what `chunk_id` is derived from), but **false** for `markdown_hash_xx64`, `splitter_version`, and especially **`span`**.
`span` looks stable but is "where the chunk sits _in this markdown_": an unchanged chunk keeps its `chunk_id` yet shifts offset when a _preceding_ chunk's length changes.
Storing any of these on the shared row is a first-writer lie (it reports whichever generation wrote the row first).
So generation-varying facts belong on the `chunk_run` and its manifest, and the shared identity row stays honest.

The manifest is **not a currency cache** — it is the chunking stage's record of which chunks each generation produced.
Chunking is deterministic, so a generation's emitted set is _rebuildable_ by re-splitting its markdown; but rebuilding a **superseded** generation means running its `splitter_version`'s behavior on the retained blob, which makes the splitter + blob store a permanent dependency of every historical audit query.
Recording the manifest once, immutably, makes "what did generation G produce?"
a query rather than a recomputation that rots when `splitter_version` moves on.
Round-trip fidelity of the emitted `Chunk` is preserved by `chunk ⋈ chunk_run_manifest ⋈ chunk_run`, not by the `chunk` row alone.

**Alternatives considered:**

- **Per-generation `chunk` rows (no dedup); generation key as a column on the chunk.**
  Makes a scalar `derivation_key`/`markdown_hash` filter a correct currentness test — but only by abandoning content-addressed identity: `chunk_id` stops being a unique key, downstream FKs retarget, and dedup of unchanged sections is lost.
  Coherent, but a larger pivot than the guarantees require; rejected in favor of stable identity + the manifest.
- **Generation-varying facts (`markdown_hash`, `splitter_version`, `span`) on the shared `chunk` row.**
  The first-writer lie above; a shared unchanged chunk reports a stale generation.
  Rejected.
- **Drop the manifest; recompute the current set by re-splitting.**
  Works for the _current_ generation but not a _superseded_ one without retaining every historical splitter version, and couples every audit query to the splitter + blob store.
  Rejected.
- **Run-scoped `chunk_id` (hash includes `run_id`).**
  Breaks content-addressing and the ADR-005 churn signal; an unchanged section would change identity every re-chunk.
  Rejected.

### Decision: ContextualizationRunScopedRecordsAsRevisionWithDerivationKeys

**Chosen:** Contextualization records **two source-scoped runs** per document — a **summary run** producing the `document_summary`, and a **variant run** producing one `contextualized_chunk` per chunk — each keyed `(stage, scope_key = str(aizk_uuid))` and superseding independently.
Splitting them is required by the spec: the summary is idempotent on `(markdown hash, summary_version, summary prompt hash, model profile)` and the variant on `(summary identity, ordered chunk identities, splitter_version, 2p/1n window policy, context prompt hash, model profile, context_version)`, so a standalone `context_version` bump must regenerate variants **without** producing a duplicate summary under unchanged summary inputs — which a single shared run cannot do.
Because contextualization output depends on model + inputs (not just source content), these records are **run-scoped** and carry a `derivation_key`: the summary run's derivation key includes the source markdown hash, `summary_version`, summary prompt hash, and model profile; the variant run's includes the summary identity, ordered chunk identities, **`splitter_version`** (so a re-chunk under a new splitter supersedes the variants even when the markdown is unchanged), the 2-prior/1-next window policy, contextualization prompt hash, model profile, and `context_version`.
Provenance pointers — distinct from the derivation key — are recorded as locators: the summary run records the consumed `conversion_output_id` and `markdown_hash_xx64` (it reads the Markdown directly; no `splitter_version`); each `contextualized_chunk` records `summary_run_id` **and** `chunking_run_id` (the exact `chunk_run` whose manifest it read, since a `chunk_id` appears in many generations) plus its `chunk_id`.
These locators are kept **out** of derivation keys (the no-surrogate rule).
The variant stores the model's **self-contained revision** of the chunk (or an empty string when the chunk is already self-contained); the raw chunk row is never written and stays the cited source-faithful unit.
A change to a run's derivation-key inputs (new markdown, changed chunk set, splitter/prompt/profile change, version bump) yields a new run of that kind that supersedes the prior; unchanged inputs + versions reuse the active run.

**Rationale:** Keying only on `(document, summary_version)` / `(chunk_id, context_version)` is insufficient — the same document id with new markdown, or the same working chunk with changed neighbors, must produce new output under an unchanged model/prompt version.
The `derivation_key` makes idempotency mean "unchanged inputs **and** version," which is the correct condition.
Storing the revision (not a delta) is required because contextualization **dereferences**: it resolves references inline, which an additive prefix cannot express — a prepended blurb leaves the unresolved pronoun in the body.
The revision is therefore genuinely different text the extraction stage needs, not a redundant copy.
Drift is not a concern: chunks are immutable and content-addressed, so a changed chunk yields a new `chunk_id` and a new variant run; the revision is always pinned to an unchanging raw chunk.

**Alternatives considered:**

- **Key only by `(document, summary_version)` / `(chunk_id, context_version)`.**
  Misses input changes under a stable version — stale output silently treated as current.
  Rejected (this was the original gap).
- **Store only an additive context blurb (a delta) prepended at use time.**
  Cannot dereference — the unresolved reference stays in the body, so the variant is not self-contained as the spec requires.
  Rejected: it resolves the prompt/mechanism mismatch by abandoning the dereferencing requirement.

### Decision: LLMContextualizationWithStubbableClientAndQualityEval

**Chosen:** Summary (one pass per document) and contextualization (one pass per chunk, framed with instructions, the document summary, two prior chunks, the working chunk, and one following chunk) call **`pydantic-ai`** (ADR-004 model provider) behind a thin injected client interface owned by the graph stage.
Tests inject a stub client, so the persistence, provenance, derivation-key, idempotency, supersession, and mode-independence contracts are exercised deterministically.
The prompt asks the model to **rewrite the working chunk into a self-contained passage**, resolving every outside reference inline, grounded strictly in the provided summary and neighbor chunks, adding/dropping/altering no claim, and leaving unresolvable references unchanged rather than guessing.
Prompt data is JSON-serialized and delimiter-looking source text is escaped, with instructions to treat source fields as untrusted data.
The _quality_ of reference resolution and faithfulness (no added/dropped/altered claims) is measured by an offline evaluation against a gold set, not asserted on live model output (see Verification Waivers).
Contextualization is a **toggleable stage**, and the input actually used is recorded, enabling a raw-vs-contextualized comparison downstream.
Empty revision output is allowed and means the chunk was already self-contained, so the consumed text is the raw chunk unchanged; a revision past the chunk-relative length budget is rejected before persistence.

**Rationale:** The deterministic contracts must be testable without a live model; an injected client makes that clean.
Situating-context quality is an output property of a non-deterministic model — its honest evidence is an eval distribution, not a unit assertion.
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
This stage resolves it once from the work-unit's locator: `conversion_output_id → ConversionOutput.aizk_uuid`, carried onto the chunking/summary/variant runs and their transition events so a source's progress is resolvable across stages.
All three runs share one `scope_key = str(aizk_uuid)` (the source); `conversion_output_id` is only a locator, never a scope.

**Generic lifecycle mapping.**
A contextualization work-unit (summarize a document, contextualize its chunks) maps onto the runtime's generic lifecycle: `succeeded` on completion; `failed` classified `retryable` on transient model/IO errors and `permanent` on malformed input; `cancelled` and `timed_out` per the harness.
Contextualization runs in-process (no subprocess isolation), so the subprocess-specific termination guarantees do not apply.

**Transaction ownership and at-least-once.**
The runner owns a `BEGIN IMMEDIATE` transaction only for `claim_next` / `finalize` — the work-unit status transition and its co-committed `pipeline_events` row.
`execute` runs **outside** that transaction, so the stage adapter opens its **own** `BEGIN IMMEDIATE` to commit the domain writes (the chunking, summary, and variant runs with their chunk/manifest/summary/variant rows).
The work-unit's terminal transition therefore commits separately from the domain writes; if the process dies between them the unit is recovered and `execute` re-runs.
Re-execution is safe because the domain write path is idempotent on its derivation keys — an unchanged document reuses its active runs (chunking included: an unchanged `derivation_key` and manifest reuse the active chunking run rather than re-superseding) and produces no duplicate chunk, summary, or variant (including the zero-chunk case, where the matching active run is reused rather than re-superseded).
A guardrail rejection (summary or revision over budget) raises before any run is recorded, leaving no partial state; the adapter maps transient model/I-O failures to `retryable` within the attempt cap and unprocessable input or output to `permanent`.

**Alternatives considered:**

- **Build a contextualization-specific worker + UI here.**
  Triples reliability machinery across stages — the duplication the refactor exists to prevent.

### Decision: ContextualizationWorkUnitMirrorsConversionJob

**Chosen:** The graph stage owns a status-bearing work-unit table — one row per document to process (chunk-persist **and** contextualize; scope: the source, `aizk_uuid`) — mirroring the conversion stage's `conversion_jobs`.
Following the identity vocabulary above, a work-unit carries:

- `id` — the local claim handle (a row surrogate); never a derivation input.
- `idempotency_key` — the enqueue dedupe key, so re-enqueueing the same document (incremental re-ingest, or a backfill that overlaps an already-queued unit) reuses the open unit instead of creating a second one.
- `conversion_output_id` — the local artifact locator; used to fetch the Markdown the unit then splits and persists.
- `aizk_uuid` — the durable source identity, resolved once from the conversion output and carried onto the chunking/contextualization runs and the work-unit's transition events so a source's progress is resolvable across stages.
- a lifecycle status, an attempt count, and retry-scheduling fields.

The stage adapter implements the runtime's `StageHandler` over this table: `claim_next` selects the oldest eligible unit and transitions it to `running`, `execute` runs the unit-of-work, and `finalize` transitions it to a terminal status.
Both run modes enqueue rows into this table, deduped on `idempotency_key`.

**Unit-of-work: the full graph write path.**
`execute` runs the whole path for one document: fetch the Markdown via `conversion_output_id`, run `aizk.chunking.split` and `persist_chunks` to record (or reuse) the chunking run and chunk rows, then summarize the document and contextualize its chunks — read back in **document order (`ordinal`)** rather than the `chunk_id`-ordered manifest the chunk store returns.
Chunk persistence and contextualization are one required pipeline, not separately-gated steps: the contextualization toggle is an enqueue/eval lever — bounding the first backfill to a corpus sample and supporting the downstream raw-vs-contextualized comparison — not a per-unit branch that emits chunks without variants.

**Locator vs. derivation split.**
`execute` resolves inputs by _locator_ (`conversion_output_id` → Markdown) but derives reuse/supersession by _content_: the chunking, summary, and variant runs' `derivation_key`s come from `markdown_hash_xx64`, the version stamps, the prompt/model derivation keys, the summary content identity, and the ordered `chunk_id`s — never from `id`, `conversion_output_id`, or any other local row handle.
This keeps the no-surrogate rule (a derivation key is reproducible across databases with different inserted row ids) intact through the worker path.

**Rationale:** The runtime's claim/lease/retry/stale-recovery machinery operates over a stage-owned work-unit table with a durable status (the `StageHandler` contract); the conversion stage is the working precedent.
A worker cannot lease work without a durable `running` marker, so an explicit work-unit table is required — deriving eligibility from "converted artifacts lacking an active contextualization run" alone gives no claim marker and admits concurrent double-claims under the serialized writer, and no place to record attempts, retry-wait, or a per-document terminal status for the operator view.
Mirroring `conversion_jobs` keeps the operator and developer mental model identical across stages (submit → queued → running → terminal, with retry/cancel).

**Alternatives considered:**

- **No work-unit table; derive eligibility from run state.**
  No durable claim marker, so two workers can claim the same document; nowhere to record attempts, retry-wait, or a per-document terminal status.
  Rejected.
- **Reuse `conversion_jobs`.**
  Conflates two stages' lifecycles in one table and couples their schemas; the runtime expects a per-stage work-unit store.
  Rejected.

### Decision: RunModeAffectsBatchingOnly

**Chosen:** The per-document processing unit is the worker-driven stage's unit-of-work (split and persist the chunks, summarize the document, contextualize its chunks).
The two run modes are **enqueue patterns**, not separate write paths: bulk/backfill enqueues work-units for many documents (throttled background work, batched per-document commits); incremental enqueues one work-unit when a document is ingested.
Both feed the **single** write path — the stage adapter's `execute`, claimed and driven by the shared runner — so the active run's record set and provenance are identical; only enqueue volume and scheduling differ.

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
  graph work-unit execute  (one path; bulk batches, incremental per-doc; all runs scoped by aizk_uuid)
        │
        ├── chunk_run (derivation_key = markdown_hash + splitter_version, status active|superseded)
        │      ├── chunk_run_input    → consumed conversion_output_id (locator) + markdown_hash (verify)
        │      ├── chunk rows          (content-addressed, immutable, stable facts only: id/source/text/hash/heading/ordinal)
        │      └── chunk_run_manifest  (append-only: run_id, chunk_id, span — the chunks this run produced)
        │
        ▼
  LLM (pydantic-ai, stubbable)
        ├── summary run  (derivation_key: markdown/prompt/model; provenance: conversion_output_id)
        │      └── document_summary
        └── variant run  (derivation_key: summary+chunks+splitter_version+window/prompt/model+context_version)
               └── contextualized_chunk  (self-contained revision, empty ⇒ already self-contained;
                                          provenance: summary_run_id + chunking_run_id + chunk_id)
        │
        ▼
  pipeline_events log  (append-only work-unit lifecycle;
                        each event carries run_id + aizk_uuid)

invalidation = run.status active→superseded (the one blessed mutation; a bare pointer flip, not evented); rows immutable.
run lineage  = supersedes_run_id chain (append-only)
backward trace = contextualized_chunk → chunking_run_id → chunk_run_manifest(chunk_id, span) + chunk(text)
                 → chunk_run(markdown_hash, conversion_output_id) → aizk_uuid
consume-at-use:  stored revision  (or raw working chunk when the revision is empty)
                 (raw chunk text never modified; it stays the cited unit)
```

All graph tables live in the conversion SQLite database; one serialized writer; Litestream replicates the WAL.

## Risks

- **LLM cost of per-chunk contextualization.**
  Mitigation: toggleable stage, derivation-key / content-hash caching, a bounded corpus sample for the first backfill.
- **Revision quality and faithfulness are model-dependent, not guaranteed.**
  Because the variant is a model rewrite, the model can in principle alter a claim, drop a qualifier, or hallucinate a referent — a larger surface than an additive prefix.
  Mitigation: the raw chunk stays canonical, immutable, and the cited unit, so the revision is a derived aid, not the source of record; grounding/no-alteration prompt constraints; structural contracts tested via stub; resolution **and faithfulness** measured by offline eval against a gold set (Verification Waivers); the raw-vs-contextualized toggle quantifies the benefit.
- **Backfill write throughput against the serialized writer (ADR-003).**
  Mitigation: batch inserts per document, throttle backfill, keep reads on WAL.
- **Superseded-run accumulation.**
  Re-chunking and version bumps grow storage with revision count.
  Mitigation: invalidation is a cheap run-status transition; the bulk content of superseded runs is reclaimed by the `artifact-compaction-retention` change (deletion by retention policy is not a mutation and does not violate row immutability).
- **Shared-database coupling.**
  Adding graph tables to the conversion migration tree couples the stages' migrations.
  Mitigation: ordinary relational data on ADR-003's documented Postgres path; a migration-ordering concern, not a data-model one.
- **Revision storage doubles the chunk body.**
  The variant holds a full revised passage, not a delta, so a contextualized corpus stores chunk-sized text twice.
  Mitigation: accepted — the revision is genuinely different (dereferenced) text the extraction stage needs, not a redundant copy; an empty revision (already-self-contained chunk) stores nothing; superseded-run content is reclaimed by `artifact-compaction-retention`.

## Verification Waivers

- **Requirement:** The contextualized variant is a self-contained revision (`chunk-contextualization`).
  **Reason:** Reference-resolution quality and faithfulness (resolving outside references inline while adding/dropping/altering no claim) are properties of non-deterministic LLM output; there is no deterministic automated execution that proves the model resolves every reference and preserves every claim on arbitrary corpus text.
  The _persistence, provenance, guardrails, and derivation-key_ of the variant are tested deterministically with a stubbed client; the _resolution and faithfulness quality_ is the waived dimension.
  **Manual evidence:** Offline contextualization-quality evaluation against the graph-stage gold set (established with the canonicalization validation-gate work); recorded as an eval report under `data/graph_research/` when the gold set lands.
  **Recorded:** 2026-05-22

## Verification Overrides

- **Finding:** The `chunk-contextualization` "self-contained revision" waiver's declared manual evidence (an offline contextualization-quality eval against the graph-stage gold set, under `data/graph_research/`) does not yet exist, so by `sdd-verify` evidence-rules §2 the waiver is not currently checkable and the waived quality dimension is a blocking gap.
  **Stage:** verify **Reason:** The waived dimension — the LLM's reference-resolution and faithfulness _quality_ on arbitrary text — is non-automatable, and the eval that measures it depends on the graph-stage gold set produced by the downstream canonicalization validation-gate work, which has not landed.
  Every automatable contract of the variant (persistence, provenance, separate addressability, derivation key, guardrails, empty-revision-consumes-raw, backward traceability) is TESTED.
  Proceeding to `sdd-sync` is justified so the persistence/contextualization substrate is available to the dependent mention-extraction change without waiting on the gold set.
  **Constraints:** `sdd-sync` SHALL NOT record the variant's reference-resolution/faithfulness quality as verified — only the structural contract is verified.
  No test may be skipped, `xfail`'d, or weakened to mask the gap.
  This override is void once the eval lands and the waiver's **Manual evidence** line is updated to point at the actual report.
  **Follow-up task:** "Land the offline contextualization-quality eval against the graph-stage gold set; update design.md § Verification Waivers Manual-evidence to cite the report" (unchecked in `tasks.md` § Deferred verification).
  **Approved by:** user **Recorded:** 2026-06-03
