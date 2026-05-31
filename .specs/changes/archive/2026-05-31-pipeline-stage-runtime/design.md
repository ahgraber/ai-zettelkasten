# Design: Pipeline-Stage Runtime

## Context

The conversion stage built reliability-critical machinery for running queued work — a worker runner (loop, orchestrator, shutdown, supervision), a same-transaction transition event log (`record_transition`), and an HTMX operator UI.
Chunk contextualization, mention extraction, and later canonicalization each need the runner and the run/dataset-version model.
Re-implementing them per stage would triple the surface for bugs the conversion archive already fixed (graceful shutdown, concurrency, backpressure, stale recovery, startup validation).

This change extracts the **primitives** into a new `aizk.pipeline` package and re-points conversion as the first consumer.
It is deliberately **not** a framework: the three concerns being generalized have very different maturity.
The runner is mature in conversion; the run/dataset-version primitive comes mostly from graph-stage needs and has one real consumer so far; the operator UI is the least-proven (one instance).
Fusing all three into a single framework would freeze a leaky abstraction before a second consumer proves the seam.
So:

- the **runner** generalizes over a stage-supplied **handler protocol** rather than a universal work-unit table;
- the **run/dataset-version primitive** is separable from work-unit execution;
- the **operator UI is out of scope** (deferred until a second consumer proves the shape — see proposal).

Constraints:

- **Database (ADR-003).**
  SQLite + SQLModel/Alembic, WAL, a single serialized writer, Litestream.
  The serialized writer makes the run-supersession transaction naturally atomic.
- **Conversion's job model is rich and stage-specific.**
  `ConversionJob` carries owner-scoped idempotency, source-ref submission semantics, upload phases, output links, and legacy API compatibility.
  None of that generalizes; it stays in `aizk.conversion` behind the handler protocol.
- **Behavior-preserving for conversion.**
  Conversion's observable behavior and its test suite are the regression net; the move is structural.
  New primitives (run record, handler-protocol seam, shared event table) are additive surface for future stages.

## Decisions

### Decision: PrimitivesPackageOverRepositoryProtocol

**Chosen:** `aizk.pipeline` exposes (a) a runner that drives discovery/claim/transition/cleanup through a `StageHandler` protocol each stage implements over its own tables, (b) a run/dataset-version primitive, and (c) a `record_transition` helper.
There is **no universal `work_units` table**; each stage owns its work-unit tables and identities.

**Rationale:** Conversion's `ConversionJob` semantics (owner idempotency, source-ref, upload phases, output links, legacy API) do not generalize, and contextualization/extraction need different unit identities and retry surfaces.
A protocol-backed runner over stage-owned tables lets each stage keep its schema while sharing the loop, draining, cancellation, timeout, and recovery logic.
This is the Rule-of-Three discipline: extract the proven seam (the runner), not a speculative central model.

**Alternatives considered:**

- **Universal `work_units` table the stages conform to.**
  Forces migrating conversion's rich job semantics into a shared schema now (or building a leaky superset).
  Rejected as premature.
- **Full runtime framework (runner + run + UI fused).**
  Couples three concerns of different maturity; freezes the least-proven (UI) abstraction first.
  Rejected.

### Decision: StageHandlerAndAdapterResponsibilities

**Chosen:** The seam is narrow, but not engine-neutral method-by-method.
The **runner is the current orchestration engine implementation**: it owns work discovery, claim/lease, concurrency, eligibility/submission ordering, retry-wait scheduling, signal handling + graceful drain, wall-clock timeout enforcement, graceful-before-forceful termination, stale-unit recovery scheduling, and observability.
The **stage adapter/`StageHandler` owns**: startup dependency validation, the stage-specific query/transition shape over its own store, the unit-of-work execution, mapping its execution result to a generic terminal outcome + retryable/permanent classification, cancellation hooks, transient-resource cleanup, timeout/concurrency declarations, status-transition writes through the shared event helper, and the run `scope_key`.
Optional per-stage subprocess isolation is a capability the adapter opts into.

**Rationale:** Pinning the seam now (rather than discovering it during implementation) is what keeps the runner generic and the abstraction honest — it was the review's explicit ask.
The durable portability line is not "every `StageHandler` method survives an engine swap."
Discovery/claim/lease/eligibility/stale-recovery are engine-owned and would be replaced by honker, procrastinate, absurd, Restate, Temporal, or a similar tool.
The pieces intended to survive are the functional core (execute + classify), the stage-owned transactional state writes, the run/dataset-version primitive, and the event projection.
The split follows functional-core/imperative-shell: the adapter is the testable unit-of-work + mapping logic; the runner is one I/O shell.

**Alternatives considered:**

- **Thin adapter (just a `run()` callable).**
  Pushes retry/cancel/cleanup/scope back into the runner, which then needs stage-specific knowledge — defeating the protocol.

### Decision: RunPrimitiveScopeKeyAndAtomicSupersession

**Chosen:** A `run` record keyed by `(stage, scope_key)` carries version stamps, `input_fingerprint`, `supersedes_run_id`, and `status`.
The stage defines its own `scope_key` (chunking: per converted-artifact; contextualization: per document; mention extraction: corpus-wide).
Activating a new run and superseding the prior is one transaction, enforced by a partial unique constraint ("at most one active run per `(stage, scope_key)`"); SQLite's serialized writer makes this atomic without extra locking.
The primitive is independent of work-unit execution — a stage may record runs without using the runner, and vice versa.
Row-identity scoping is the adapter's choice: content-deterministic stages use content-addressed ids + an append-only run-membership table; model/input-dependent stages use run-scoped ids.

**Rationale:** Keying on `(stage, scope_key)` resolves the review's "scope is under-specified" point — the invariant is meaningful only per a stage-defined scope.
Atomic supersede prevents the two-active / zero-active windows the review flagged.
Keeping the primitive execution-independent lets chunking (no LLM, fast) and the LLM stages share it without coupling.

**Alternatives considered:**

- **Global "one active run" with no scope.**
  Wrong for per-document/per-chunk stages.
- **Eager supersede then insert (two transactions).**
  Opens a no-active-run window and a concurrency race.

### Decision: GenericLifecycleAndRetryClassification

**Chosen:** A generic lifecycle — `queued → running → {succeeded, failed, cancelled, timed_out}` — with failed outcomes classified `retryable | permanent`.
The adapter maps its execution result onto this set; the runner reasons about progress, eligibility, and retry uniformly.
Retry eligibility = retryable-failed and past its retry-wait.

**Rationale:** The review noted "cancelled / timeout / failure" weren't mapped to statuses or retry semantics.
A single generic state machine gives the runner a uniform basis for scheduling and the operator surface, while stage-specific statuses (conversion's `FAILED_RETRYABLE`/`FAILED_PERM`/`UPLOAD_PENDING`, etc.) map onto it.

### Decision: SharedTransitionEventTableKeyedBySourceIdentity

**Chosen:** A single `pipeline_events` table holds append-only transition events for all stages, each row carrying the stage, work-unit reference, run reference, and the `aizk_uuid` source identity, written in the same transaction as the status change via the `record_transition` helper.
Cross-stage timelines are then a query by source identity with no per-stage joins.

**Rationale:** The conversion event log already denormalized `aizk_uuid` "so future processing-stage event tables that share the same Source identity can be queried alongside" — a shared table makes that first-class.
Porting conversion onto it is a structural relocation of `conversion_job_events`, covered by the migration-equivalence and event tests (behavior preserved: the same events are recorded).

**Alternatives considered:**

- **Per-stage event tables + a union view.**
  Avoids touching `conversion_job_events`, but every cross-stage query goes through a maintained view and new stages each add a table.
  The shared table is simpler for the cross-stage timeline the design wants.

### Decision: PackageHomeAndStranglerSequencing

**Chosen:** Primitives live in a new top-level `aizk.pipeline`; `aizk.core` stays low-level shared (`database.py`).
Sequencing is strangler-style and behavior-preserving: (1) build the primitives + runner with a stub repository under new tests; (2) implement conversion's `StageHandler` over `ConversionJob` and route its transitions through the helper; (3) move the runner logic out of `aizk.conversion.workers` into `aizk.pipeline`, deleting the conversion-local duplicates, keeping conversion's suite green; (4) **last**, reconcile the specs — relocate the now-duplicated generic contracts out of `worker-process-management`/`conversion-worker` into `pipeline-stage-runtime`.

**Rationale:** Consumers import the runtime, not vice versa, so a new top-level package keeps the dependency direction clean.
Doing the spec reconcile last keeps the risky structural move separate from the spec migration (modularity-skill rule: never mix behavior and structure — here, never mix structural relocation and spec churn).

### Decision: StageHandlerProtocolSurface

**Chosen:** The `StageHandler` protocol a stage implements is, concretely (signatures pinned now so the runner can be coded against them):

- `validate_dependencies() -> None` — raise on missing required dependencies (startup gate).
- `claim_next(session) -> WorkUnitHandle | None` — within a caller-opened `BEGIN IMMEDIATE` transaction, select the next eligible work-unit in the stage's submission order, transition it to `running` via the helper, and return an opaque handle (or `None` if none eligible).
  The **runner owns** the session and transaction boundary; the handler runs its stage-specific eligibility query and transition inside it.
  If an outside engine is adopted, this eligibility/claim portion is replaced by the engine, while the transition/event projection remains a stage-owned write.
- `recover_stale(session) -> list[WorkUnitHandle]` — batch-transition units stranded in `running` back to eligible, recording the recovery cause.
  This is part of the current SQLite runner, not a domain contract future engines must expose.
- `execute(handle) -> StageResult` — run the stage's unit-of-work (no DB writes to the unit's status; pure-ish work + side effects the adapter owns).
- `map_result(result | exception) -> (TerminalOutcome, retry_class)` — map success/exception to the generic terminal outcome + `retryable | permanent`.
- `finalize(session, handle, outcome) -> None` — transition the work-unit to its terminal status via the helper.
- `cleanup(handle) -> None` — release transient resources; called on every terminal outcome.
- `cancel(handle) -> None` — cooperative cancellation hook.
- properties: `timeout`, `concurrency_limit`, `scope_key(handle)`, and an `isolation` flag (in-process vs subprocess).

**Rationale:** The review's blocking question was that the seam was described by responsibility, not signature.
Pinning it now lets task group 1 produce a codeable protocol.
The runner owning the session/transaction (and passing it into `claim_next`/`finalize`) preserves conversion's existing "caller owns `BEGIN IMMEDIATE`, helper does not commit" convention, so the co-commit guarantee holds across the extraction.
This protocol is the contract for the current embedded engine; it is deliberately not a universal adapter API for every possible orchestrator.

**Alternatives considered:**

- **Repository owns its own session/commit.**
  Breaks the same-transaction co-commit guarantee and the single-serialized-writer discipline; rejected.

### Decision: ExecutionModelIsThreadPlusOptionalSubprocess

**Chosen:** The runner preserves conversion's **thread-pool + optional subprocess** execution model rather than rewriting to asyncio.
In-process units run in the runner thread pool (the graph stages' LLM/NER calls are blocking I/O, fine in threads); a stage may opt into subprocess isolation (conversion's docling path).
The subprocess-shaped guarantees (graceful-before-forceful, no-orphan-descendants) apply only to subprocess-isolated stages (see the spec's bounded-execution requirement); in-process units are terminated by cooperative cancellation + cleanup.
Runner tests wrap the act phase with `no_thread_leaks`, and with `no_task_leaks` only where a path uses asyncio.

**Rationale:** "Behavior-preserving" for conversion is far cheaper and lower-risk on the existing thread+subprocess model than an async rewrite; the new stages don't need asyncio.
This resolves the review's "thread vs async, and the spec asserts subprocess-shaped termination for in-process stages" contradiction.

**Alternatives considered:**

- **Async runner.**
  Would make the conversion port a behavior-changing rewrite (subprocess supervision, `ThreadPoolExecutor`, signal handling all change), violating the regression-net premise.

### Decision: PipelineEventsGenericSchema

**Chosen:** `pipeline_events` columns are generic: `event_id`, `stage`, `work_unit_ref` (text — each stage's unit identity), `run_id` (nullable), `aizk_uuid`, `from_status` / `to_status` (text), `kind` (text), `attempt` (nullable int), `occurred_at`, `payload_json`.
Conversion's typed columns are genericized on relocation: `ConversionJobStatus` → text status, the conversion `kind` enum → text, and the `job_id` integer FK (`ON DELETE SET NULL`) → `work_unit_ref` plus the already-denormalized `aizk_uuid` (which is what preserved the post-deletion audit).
The migration-equivalence test asserts the **same events (values) are recorded** for a conversion job before and after, not column-type identity; the existing schema-parity, FK-set-null, and enum-drift tests are updated to assert the generic shape (the audit-survives-job-deletion behavior is preserved via `aizk_uuid`, no longer via a DB FK).

**Rationale:** A shared cross-stage table cannot keep conversion-specific enum/FK typing (the review's F1).
Genericizing to text + a `work_unit_ref` + the denormalized `aizk_uuid` keeps every event queryable by source identity across stages and preserves the operator-deletion audit guarantee without a stage-specific FK.

**Alternatives considered:**

- **Per-stage event tables + union view** (recorded earlier as the alternative): avoids touching `conversion_job_events`, but every cross-stage query goes through a maintained view and each stage adds a table.

### Decision: SharedMetadataConversionAlembicTreeOwnsAllTables

**Chosen:** `aizk.pipeline`'s `run` and `pipeline_events` models register on the shared `SQLModel.metadata`, and the **conversion Alembic tree** (`src/aizk/conversion/migrations`) owns all tables — conversion, pipeline, and the graph tables added by `chunk-persistence-contextualization` and `mention-extraction-foundation` — as one linear migration history.
`aizk.pipeline` stays import-independent of `aizk.conversion` (conversion imports pipeline, not vice versa); the migration _tree_ is shared deployment config, not a code dependency.
The schema-parity test (migrated ≡ `create_all` over the single metadata) continues to hold.

**Rationale:** ADR-003 is one SQLite file with one serialized writer and one Litestream target, so one metadata and one linear Alembic history is correct; splitting envs would fragment the parity guarantee.
This resolves the review's most-under-specified, critical-path item (the proposal's own open question).
The three changes' migrations must land as one linear head — sequence them in the build order (runtime → A → B).

**Alternatives considered:**

- **Separate Alembic env for `aizk.pipeline`.**
  Two histories over one database file; breaks the single-metadata parity test and complicates ordering.
  Rejected.

### Decision: ConversionIsNotRetrofittedOntoRuns

**Chosen:** The run/dataset-version primitive is **additive** — conversion is **not** retrofitted onto it in this change.
Conversion keeps its `ConversionJob` model and owner-scoped idempotency as-is; the port relocates only the **runner** and the **transition events** (`conversion_job_events` → `pipeline_events`).
The run primitive is built and tested against a stub repository and is first consumed by the graph stages.
Conversion therefore records no run: it never calls the run primitive, so no `run` row exists for a conversion job.
`ConversionStageHandler` still implements the protocol's `scope_key(handle)` property (returning the job id, a per-job scope) because the `StageHandler` surface requires it, but that key is never used to open or supersede a run — it is inert for conversion.

**Rationale:** Conversion has no dataset-version semantics to preserve, and retrofitting runs onto it would be a behavior change, not a structural move.
Keeping the run primitive additive is what lets the graph stages (the real consumers) validate it before the abstraction freezes — and it answers the review's "what is conversion's `scope_key`?"
(it has a protocol-required per-job key, but records no run, so no dataset-version semantics ride on it).

## Architecture

```text
                aizk.pipeline (primitives)
   ┌───────────────────────────────────────────────┐
   │  runner: claim/lease · concurrency · drain ·  │
   │  cancel · timeout · stale-recovery · observ.   │
   │                    │                           │
   │         StageHandler protocol               │   run primitive            record_transition
   │   (discover/claim/transition/cleanup/validate) │   (run: (stage,scope_key), │   helper
   └───────────────────│───────────────────────────┘    status, atomic supersede)│
                       │  implemented per stage                                    ▼
        ┌──────────────┼───────────────┬───────────────────┐            pipeline_events
        ▼              ▼                ▼                   ▼            (shared, append-only,
   conversion     contextualization  extraction      (future stages)     keyed by aizk_uuid)
   (ConversionJob  (graph tables)    (mention tables)
    stays here)
```

Conversion is the first adapter; its `ConversionJob` table, idempotency, source-ref, upload phases, and output links stay in `aizk.conversion` behind the handler protocol.

## Risks

- **Premature abstraction.**
  Mitigation: primitives not framework; runner over a protocol (no central table); UI deferred until a second consumer; the graph stages are the second real consumer validating the run primitive and runner seam before the abstraction is frozen.
- **Conversion regression during the port.**
  Mitigation: conversion's existing test suite is the regression net; strangler sequencing; structural-only commits separate from any behavior change.
- **`conversion_job_events` → `pipeline_events` relocation.**
  Mitigation: migration-equivalence test and the existing event-log tests assert the same events are recorded; the relocation is structural.
- **`scope_key` correctness across stages.**
  Mitigation: stage-defined `scope_key` with the one-active-run invariant enforced by a partial unique constraint, exercised by concurrent-run and failed-supersession tests.
- **Runner concurrency/lifecycle bugs.**
  Mitigation: runner tests wrap the act phase with `pyleak` (`no_task_leaks` / `no_thread_leaks`) per the project testing rule, plus drain/cancel/timeout/stale-recovery cases against a stub repository.

## Decision Record

The architectural decision (a shared pipeline-stage primitives package) is recorded as an addendum to `docs/decision-record/009-orchestration.md` (orchestration is the nearest neighbor), treated as a brainstorming/design pass rather than a separate gate.
Per the project's working stance, the ADR may lag the specs; it does not block this change.
