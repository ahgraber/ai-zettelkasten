# Design: Pipeline Work Admission

## Context

Work-units for the graph stages are created only by their enqueue primitives (`enqueue_document` / `enqueue_output` for contextualization, `enqueue_extraction` for extraction), and nothing in production calls them.
This change adds admission — deriving and creating the work that should exist — without disturbing what already works: workers claim directly from the database under `BEGIN IMMEDIATE` and drain independently of any service process; enqueue deduplicates on `idempotency_key`; run-level provenance records consumed upstream generations via derivation keys.

Constraints that shape the design:

- `aizk.conversion` must not depend on `aizk.graph`; the dependency direction is downstream-only.
- The single-writer SQLite/Litestream posture stays; commits stay short and batched by logical unit.
- Contextualization is LLM-backed: every admitted unit is external inference spend.
- Extraction's work-unit identity is per source (`source:{source_id}`) and terminal units are never re-enqueued; automating re-extraction on upstream change was explicitly deferred by the extraction stage.
  This change honors that deferral: extraction admission covers never-extracted sources only, and re-extraction is operator-initiated.

```text
User POSTs (conversion submission)
  ↓
Conversion service creates ConversionJob (QUEUED) in database
  ↓
Conversion Worker Process:
  └─ Claim & execute → writes ConversionOutput
  ↓  (state: conversion outputs)
Contextualization Worker Process:
  ├─ Admission loop (on timer, if enabled):
  │  └─ "Scan for sources whose NEWEST output has no work unit"
  │     → Create contextualization work units (up to capacity)
  └─ Work-claiming loop:
     └─ Claim & execute → writes chunks + chunking run + contextualizations
  ↓  (state: active chunking runs)
Extraction Worker Process:
  ├─ Admission loop (on timer, if enabled):
  │  └─ "Scan for sources with an ACTIVE CHUNKING RUN and no extraction work"
  │     → Create extraction work units (up to capacity)
  └─ Work-claiming loop:
     └─ Claim & execute extraction work
```

## Decisions

### Decision: AdmissionLoopInWorkers

**Chosen:** Each stage's existing worker process runs its own admission loop — the contextualization worker admits contextualization work, the extraction worker admits extraction work — on a configured interval, gated by a per-stage enable flag.

**Rationale:** There is no shared composition root that enumerates stages; each worker wires only its own handler and runner, and admission inherits that shape.
No new process, no new supervision, no cross-stage registry.
Pausing admission without stopping execution — the operational reason to want a separate command — is served by the per-stage enable flag, which takes effect on the next loop iteration.

**Alternatives considered:**

- Separate scheduled command (`aizk-graph admit`): independently pausable, but adds a scheduled process to supervise, and the enable flag already provides the pause.
- Single shared admission service: requires a stage registry that does not exist and adds an orchestration component the project's defaults prohibit without a named missing primitive.

### Decision: CapacityAtTheEnqueueSeam

**Chosen:** The capacity check lives inside the enqueue primitives — the only places work-unit rows are constructed — evaluated after the idempotency-dedupe branch.
Bulk callers (admission pass, backfill) compute remaining headroom once per batch rather than per row.

**Rationale:** Conversion's queue-depth limit is universal because conversion constructs jobs in exactly one place with the check in front of it.
Graph has two construction sites; putting the check inside them makes every caller — intake route, admission loop, backfill command, notebook — subject to it with no bypass, and leaves the admission loop free of any dependency on the service process.
Checking after dedupe preserves conversion's contract that a duplicate bypasses the depth check: a reused unit adds no work and is returned, not refused.
Per-batch headroom avoids a `COUNT` per row on bulk paths.

**Alternatives considered:**

- Capacity in the intake route only (conversion's literal layout): leaves the admission loop and bulk commands ungated — two throttles with different semantics, or none.
- A separate per-pass admission cap: a second limit with different semantics guarding the same queue; rejected in the proposal.

**Capacity definition:** actionable backlog = units in `QUEUED` status plus failed units awaiting retry, mirroring conversion's actionable set.
The refusal raises a dedicated exception type; each surface maps it (intake → HTTP 503 + `Retry-After`; admission loop → stop admitting, log remainder; CLI → non-zero exit with message).

### Decision: IntakeCallsDomainEnqueueInProcess

**Chosen:** `POST /v1/contextualizations` and `POST /v1/extractions` are routes on the existing graph service that resolve the referenced upstream artifact, then call the stage's domain enqueue in the request transaction.
The capacity refusal maps to the same 503 + `Retry-After` response shape the conversion API returns.

**Rationale:** The throttle is already universal at the enqueue seam, so intake needs no gate of its own; HTTP indirection between the worker and the service would only break the worker's independence.
Request handling mirrors conversion's submission ordering: resolve → dedupe lookup (200 with existing unit) → capacity → create (201).
Intake adopts the graph service's existing principal handling unchanged.

**Alternatives considered:**

- Admission loop calling intake over HTTP for a single enqueue path: makes the worker depend on the service being up; rejected.

### Decision: ReadmissionIsATerminalUnitRequeue

**Chosen:** The extraction re-admission action is a status transition on the existing work-unit — terminal → `QUEUED`, attempts cleared, with a durable requeue event co-committed — declared as a stage action (`Re-extract`) on the extraction stage's console descriptor.

**Rationale:** This is the retry precedent: console retry already flips a failed unit back to queued with fields cleared and an equivalent durable event, and the console's declared-action machinery provides individual and bulk application, eligibility summaries, and pathway equivalence for free.
It preserves extraction's one-row-per-source identity, needs no schema change, and keeps generation history where it already lives — in runs and their derivation keys, where supersession is decided at execute time.

**Alternatives considered:**

- Idempotency-key rotation on the upstream generation (`source:{id}|gen:{key}`): turns the work-unit table into per-generation history, duplicating run lineage, and requires an identity migration on a shipped table.
  The extraction module named both options when it deferred the decision; rotation buys nothing here that runs don't already record.

**Eligibility:** unit in a terminal status AND source stale.
The eligibility predicate lives in the stage's action rules, where the console's mixed-selection handling already reports skipped-as-ineligible.

### Decision: StalenessFromRecordedDerivationKeys

**Chosen:** A source is stale when its active mention-extraction run's recorded `upstream_derivation_key` differs from the derivation key the current upstream state would yield.
The staleness query reuses `_resolve_upstream_derivation_key` — the same resolver extraction uses at execute time — against the source's current active chunking/contextualization runs.

**Rationale:** The extraction run already records the consumed upstream run's derivation key, so staleness is computable with no schema change.
Reusing the execute-time resolver is load-bearing: a separate staleness predicate would drift from what re-extraction would actually read, producing false stale/current verdicts.
One resolver, two consumers — the same structural move as pending-work (one query feeding both admission and display).

### Decision: PerStageAdmissionAdapter

**Chosen:** Each participating stage supplies a small admission adapter: a pending-work query (`session, limit -> keys`), the stage's enqueue callable, and its capacity configuration.
The worker's admission loop consumes the adapter; the console's `StageDescriptor` gains optional `pending_count` / `pending_list` / `stale_count` callables, feature-detected exactly like the existing `failed_split`.

**Rationale:** Matches the two registration seams that exist — per-worker wiring for execution, descriptor registration for the console — without inventing a third.
A stage that supplies no adapter is untouched; feature detection is the established console pattern.

- Contextualization's pending query generalizes `latest_output_ids_per_source` (already written for the backfill): newest output per source, anti-joined against work-units on `conversion_output_id`.
- Extraction's pending query generalizes `_sources_with_active_chunking_run`, anti-joined against work-units on `source_id`.

### Decision: ConfigurationSurface

**Chosen:** pydantic-settings fields on the graph config, per the `AIZK_<SECTION>__<FIELD>` convention:

- `admission_contextualization_enabled` / `admission_extraction_enabled` (default `False`)
- `admission_interval_seconds` (shared loop interval)
- `contextualization_queue_max_depth` / `extraction_queue_max_depth` (capacity limits; absent/0 = no limit)
- `queue_retry_after_seconds` (mirrors conversion's name and default)

**Rationale:** Off-by-default satisfies the enablement contract; per-stage flags and limits reflect the cost asymmetry between the stages; reusing conversion's retry-after vocabulary keeps one fleet convention.

### Decision: TrackGraphApiSchema

**Chosen:** Add a `graph-api-openapi` generator entry to `.specs/.sdd/schema-config.yaml` alongside the conversion one, capturing before/after snapshots for the intake routes.

**Rationale:** The graph API gains public operations; an API change with no tracked schema gives verification nothing to diff.

## Architecture

```text
                         conversion outputs / chunking runs (upstream state)
                                          │
                       ┌──────────────────┴────────────────────┐
                       │ per-stage pending-work query           │
                       │ (state-derived, no memory)             │
                       └───────┬───────────────────┬───────────┘
                               │                   │ counts/lists (read-only)
                 admission loop│                    ▼
              (in stage worker,│            console descriptors
               per-stage enable)│            pending / stale / units
                               ▼
        ┌─────────────────────────────────────────────┐
        │ enqueue seam (the only construction sites)  │
        │  dedupe on idempotency_key → reuse          │
        │  capacity check (post-dedupe) → refuse      │◀── intake routes
        │  create QUEUED unit + event                 │     POST /v1/contextualizations
        └──────────────────┬──────────────────────────┘     POST /v1/extractions
                           │                                 (503 + Retry-After at capacity)
                           ▼                          ◀── backfill / CLI / notebooks
                work-unit tables (queue)
                           │
                           ▼
             StageRunner claim → execute → finalize      (unchanged)

   re-admission (extraction): operator action on a terminal unit whose source is
   stale → requeue transition + durable event → normal claim path re-extracts
   against current upstream state; the new run supersedes the prior one.
```

## Risks

- **Capacity is approximately enforced under concurrent writers**: the count-then-insert pair is not serialized across the API process and worker process, so the backlog can overshoot the limit by a few units.
  Accepted: the limit is a throttle, not an invariant; SQLite's single-write-transaction serialization keeps drift to in-flight requests.
- **Pending/stale queries run on an interval and on console loads**: both are anti-joins over the corpus.
  At current corpus scale (thousands of sources) with indexed foreign keys this is milliseconds; if the dashboard slows, the counts move behind the existing page-load budget by computing on the stage page rather than the dashboard.
  Revisit if corpus growth changes the constant.
- **Admission racing intake or backfill**: two paths deriving and enqueueing the same work concurrently.
  Safe by construction — both funnel into the same dedupe on a unique `idempotency_key`; the loser reuses the winner's row.
- **Staleness predicate drift**: if the staleness query re-implemented upstream resolution, its verdicts could diverge from what re-extraction actually reads.
  Mitigated by reusing `_resolve_upstream_derivation_key`; a conformance test pins the two to the same result.
- **Enable-flag misconfiguration** (admission on before the backfill/calibration decisions land): default-off plus per-stage flags mean nothing runs without a deliberate config change; capacity bounds the worst case to one backlog's worth of spend.
