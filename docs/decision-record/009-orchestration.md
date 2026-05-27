# 009 - Workflow Orchestration

## Status

- December 23, 2024 - Accepted (initial)
- April 4, 2026 - Revised: SQLite task queue adopted; Prefect not pursued
- May 27, 2026 - Revised: pipeline-stage runtime addendum, capability-trigger migration criteria, Restate/Procrastinate alternatives

## Context

The AI Zettelkasten system requires orchestration for managing complex workflows including:

- Document ingestion and conversion pipelines
- Long-running parsing and extraction tasks
- Scheduled reprocessing and batch operations
- Retry logic for failed operations
- Coordination between multiple processing stages

The project is self-hosted, personal/internal in scope, and requires a solution that balances operational complexity with workflow durability.
Three self-hosted orchestration platforms were evaluated in the original decision: Temporal.io, Prefect.io, and Windmill.dev.
Later pipeline-stage research also considered honker, absurd, Procrastinate, and Restate.

### Revision Context (April 2026)

After selecting Prefect, practical implementation revealed that the project's actual orchestration needs are well within the scope of a simple SQLite-backed job queue.
The `ConversionJob` table (in the application's existing SQLite database) already provides queuing, retry tracking, exponential backoff, idempotency keys, and stale job recovery with no additional infrastructure.
Adding Prefect would introduce operational overhead — a separate server process, its own backend database, worker pool management — without a clear benefit at this scale.

### Addendum Context (May 2026)

The `pipeline-stage-runtime` change extracts the current queue machinery into `aizk.pipeline`: a harness over a stage-supplied repository protocol, a run/dataset-version primitive, and a shared transition-event projection.
This does not change the selected orchestrator.
It clarifies the boundary: the current SQLite runner is one embedded engine implementation, while stage domain code, run provenance, artifact writes, outcome classification, and product read-models should remain independent enough to survive an engine replacement.

The likely trigger for replacing the embedded runner is a missing **capability primitive**, not orchestration neatness.
Valid triggers include durable signals, human approval waits, long sleeps, schedule overlap policy, event-await, step checkpointing for expensive work, or measured scale limits.
If such a trigger arrives, choose the cheapest tool that provides the primitive; do not climb a fixed SQLite → Postgres → Temporal ladder.

## Decision

### Selected Approach

**SQLite-backed task queue** using the application's existing `ConversionJob` table is sufficient for current needs.
No external orchestration system is adopted at this time.

The pipeline-stage runtime keeps that decision but names the current runner as an embedded orchestration engine.
The engine owns work discovery, claim/lease, eligibility ordering, retry scheduling, bounded concurrency, cancellation, timeout, drain, and stale-work recovery.
Stage-owned domain contracts own unit-of-work execution, result classification, run/generation state, artifact writes, and product-facing status/event projections.

### Rationale

The `ConversionJob` model provides everything this project currently requires:

- **Job status lifecycle**: `NEW → QUEUED → RUNNING → SUCCEEDED / FAILED_RETRYABLE / FAILED_PERM / CANCELLED / UPLOAD_PENDING`
- **Retry semantics**: `attempts` counter, `earliest_next_attempt_at` for exponential backoff, `FAILED_RETRYABLE` status for transient errors
- **Idempotency**: `idempotency_key` (SHA-64) enforced at the database level
- **Stale job recovery**: worker loop detects and re-queues `RUNNING` jobs that exceed a configurable timeout
- **Observability**: timestamps (`queued_at`, `started_at`, `finished_at`, `last_error_at`) and structured error fields (`error_code`, `error_message`, `error_detail`)
- **Zero additional infrastructure**: shares the application database, replicated via Litestream

All pipeline tasks are idempotent by design.
Occasional retries are acceptable.
The throughput ceiling of SQLite is not a constraint for personal/internal use.

External orchestrators should not be introduced to make the code "more flexible."
They should be introduced only when the current engine cannot express a named primitive without brittle bespoke machinery.

### Consequences

#### Positive Impacts

- **Zero operational overhead**: no extra processes, no separate backends, no new deployment concerns
- **Single database**: job state and application data colocated, consistent, and backed up together via Litestream
- **Simple reasoning**: job state is plain SQL rows; no workflow replay semantics to understand
- **Easy debugging**: query `conversion_jobs` directly to inspect queue state

#### Potential Risks

- **No step-level checkpointing**: if a job crashes mid-execution, the entire job retries from the start (acceptable for current workloads)
- **Limited concurrency primitives**: no built-in fan-out, signals, or event-await; these would need to be built ad hoc
- **Scaling ceiling**: SQLite write throughput limits concurrent workers; at high scale a Postgres-backed queue becomes necessary
- **Current-engine coupling**: repository methods such as discovery, claim, lease, and stale recovery are part of the embedded engine shape; they are not guaranteed to survive an external orchestrator migration unchanged

#### Mitigation Strategies

- **Design for idempotency**: all tasks are safe to retry from the beginning
- **Service boundary preservation**: orchestration logic stays in the runner/harness layer, decoupled from business logic
- **Product read-model preservation**: run/generation records and transition events remain application-owned projections, not queries against an orchestrator's private history
- **Migration path**: job model is intentionally simple — a Postgres migration is straightforward (see Future Considerations)

### Alternatives Considered

#### Option 1: honker _(preferred if staying on SQLite)_

**Description**: SQLite-native durable queue, pub/sub, event streams, and cron scheduler implemented as a loadable SQLite extension.
Adds Postgres-style `NOTIFY`/`LISTEN` semantics by polling `PRAGMA data_version` every millisecond; no separate broker or daemon.

**Pros**:

- **Same file as the application database**: enqueue commits atomically with business writes in the same transaction — eliminates the dual-write problem the current `ConversionJob` table already avoids, but with proper queue primitives instead of hand-rolled polling
- **No additional infrastructure**: just a SQLite extension; works with the existing Litestream replication story
- **Multi-language bindings**: Python, Node, Rust, Go, Ruby, Bun, Elixir share one on-disk format
- **Built-in primitives**: durable queues, retries, timeouts, pub/sub, event streams, cron — replaces the bespoke status machine, retry counter, and stale-job recovery in `workers/loop.py` / `workers/orchestrator.py`
- **Low wake latency**: ~0.7 ms p50 cross-process wake without client polling
- **Decorator API** available (Huey-style) for ergonomic task definitions

**Cons**:

- Newer project with a smaller ecosystem than Prefect / Temporal
- No built-in dashboard
- Inherits SQLite's single-writer ceiling (same constraint as the current setup)

**When to select**: If workflow primitives outgrow the current hand-rolled `ConversionJob` machinery but a Postgres migration is not warranted, honker is the natural in-place upgrade.
It keeps the "SQLite as the only datastore" philosophy while replacing the bespoke polling loop with a maintained library.

#### Option 2: Procrastinate _(minimal Postgres task queue)_

**Description**: Postgres-backed Python task queue with worker processes, retries, locks, and periodic tasks.

**Pros**:

- **Postgres only**: no separate orchestration server beyond the database and worker processes
- **Mature task-queue shape**: fits "one task per work unit" without adopting workflow replay semantics
- **Exclusivity primitives**: `queueing_lock` and `lock` can prevent duplicate queued/running work for a logical key
- **Low operational overhead** once the application has already migrated to Postgres

**Cons**:

- No step-level checkpointing; a failed job re-runs from the task boundary unless the application splits stages into separate tasks or records stage results itself
- Stale/zombie-job recovery is not automatic; stalled locked jobs require an explicit periodic recovery task
- Failure forensics are SQL/CLI based; no workflow history UI
- Requires migration from SQLite to Postgres

**When to select**: If the project has already moved to Postgres and needs a maintained task queue, but not durable workflow primitives.
Keep stage artifacts, run records, and transition projections application-owned; use Procrastinate only for work dispatch and retry scheduling.

#### Option 3: absurd _(preferred if migrating to Postgres for step checkpointing)_

**Description**: Postgres-native durable workflow system.
The engine lives entirely in a single `.sql` schema applied to the database; thin SDKs handle worker logic in Python, TypeScript, or Go.

**Pros**:

- **Postgres only**: no separate orchestration server — just the database the application already uses
- **Step-level checkpointing**: tasks decompose into steps; each step result is persisted so crashes resume at the last completed step, not from the beginning
- **Lightweight Python SDK**: ~2,000 lines (vs. Temporal's ~170,000); easy to understand and debug
- **Durable primitives built in**: retries, sleep, event-await, task scheduling
- **Pull-based workers**: application code pulls from Postgres at its own pace, no push coordinator needed
- **`absurdctl` CLI**: schema init/migration, queue management, task inspection — installable via `uvx absurdctl`
- **Apache 2.0 license**

**Cons**:

- Requires migration from SQLite to Postgres
- Still early-stage (April 2026: ~1,165 GitHub stars, experimental Go SDK)
- No built-in dashboard (a separate `habitat` UI exists but is an add-on)
- Pull-based only — push/HTTP invocation requires a wrapper

**When to select**: If the project migrates to Postgres and needs step checkpointing or durable event-await without a separate orchestration server, absurd is the preferred Postgres-native workflow option.
It extends the "Postgres as infrastructure" philosophy and avoids introducing a separate orchestration server.
Installation is `uv add absurd-sdk`; schema setup is `uvx absurdctl init -d <database>`.

#### Option 4: Prefect.io _(original selection — not pursued)_

**Description**: Python-native workflow orchestration platform with a self-hosted server and worker pools

**Pros**:

- Python-native with strong developer ergonomics
- Built-in scheduling, retries, observability dashboard
- Good documentation and active ecosystem

**Cons**:

- Requires running a separate Prefect server process (plus its own backend database)
- Operational overhead is disproportionate to current workflow complexity
- More infrastructure to maintain for a personal/internal project

**Reason for not selecting**: The actual workflows fit comfortably within a simple job table.
The operational cost of Prefect is not justified.

#### Option 5: Restate

**Description**: Journaled durable-execution system with a single-binary server, durable RPC, idempotency keys, virtual objects, timers, and event waits.

**Pros**:

- **Lower starting footprint than Temporal**: a single Restate server plus worker/service processes
- **Durable execution primitives**: journaled steps, durable timers, event waits, and idempotent calls
- **Per-key serialization** via Virtual Objects, useful when one logical item must not be processed concurrently
- **Good migration target for capability triggers** such as long waits, signals/event-await, and durable external callbacks

**Cons**:

- Younger ecosystem than Temporal
- Workflow/handler orchestration code must respect deterministic replay rules; side effects belong in journaled calls/steps
- Cross-store atomicity is still unavailable: application DB writes must be idempotent if a handler commits and then fails before Restate records completion

**When to select**: If the project needs Temporal-like durable-execution primitives but Temporal's initial operational footprint is too high.
Restate is especially attractive for durable signals/event-await and per-key serialized processing while staying below Temporal-scale infrastructure.

#### Option 6: Temporal.io

**Description**: Event-sourced workflow engine with strong durability guarantees

**Pros**:

- Extremely robust exactly-once workflows with deterministic replay
- Rich primitives: signals, timers, retries, compensation
- Best-in-class durability for complex long-running state machines

**Cons**:

- Requires running the Temporal Server cluster (multiple services)
- Steep learning curve with a different programming model
- Significant infrastructure overhead — overkill for current scope

**Reason for not selecting**: Operational and conceptual overhead is not justified for a personal/internal document processing project.

#### Option 7: Windmill.dev

**Description**: UI-driven automation platform with visual workflow building

**Pros**:

- Fast setup for UI-driven automation
- Good for human-in-the-loop and operator-facing workflows
- Low-code/no-code capabilities

**Cons**:

- Not suited for heavy, code-driven, long-running pipelines
- More constrained Python integration
- Weaker programmatic workflow definition

**Reason for not selecting**: Better suited as a complementary tool for administrative flows.
May be revisited for operator-facing dashboards if needed.

## Implementation Details

**Current / in-flight setup** (SQLite task queue):

- `ConversionJob` table in the application SQLite database (via SQLModel/SQLAlchemy)
- `aizk.pipeline` harness: polling/claim loop, stale-work recovery, concurrency, timeout, cancellation, and drain
- stage repository/adapters: per-job execution lifecycle, status/event projection, and error classification
- Litestream provides continuous replication for durability

**If migrating to absurd**:

1. Migrate application database to Postgres
2. Apply absurd schema: `uvx absurdctl init -d <database>`
3. Replace `workers/loop.py` polling with an `absurd-sdk` worker (`uv add absurd-sdk`)
4. Register existing job handler logic as an Absurd task with explicit step boundaries
5. Remove `ConversionJob` table and status-machine logic (replaced by Absurd's model)

**Migration Considerations**:

- Replace the embedded engine layer, not the stage domain contracts.
- Keep application-owned run/generation state and transition projections even if the engine changes.
- Make engine/application boundary writes idempotent because external workflow engines cannot co-commit their progress marker with the application database.
- Preserve clean service boundaries and task-oriented inputs/outputs.

## Related ADRs

- [002-content-parsing.md](002-content-parsing.md): Parsing workflows are orchestrated by this queue
- [001-content-archiving.md](001-content-archiving.md): Archiving workflows use this queue for retries
- [003-database.md](003-database.md): Application database and Litestream replication strategy

## Additional Notes

**Future Considerations**:

- If queue ergonomics outgrow the hand-rolled `ConversionJob` machinery but staying on SQLite is still appropriate, adopt **honker** as an in-place upgrade
- If the project migrates to Postgres and needs only a maintained task queue, consider **Procrastinate**; wire explicit stalled-job recovery and keep stage checkpointing in application state
- If the project migrates to Postgres and needs step checkpointing, event-await, or durable sleep without a separate orchestration server, consider **absurd**
- If capability needs include durable signals, long external waits, or per-key serialized durable execution while Temporal's footprint is still too high, consider **Restate**
- If workflow complexity requires mature durable execution, rich failure forensics, signals, timers, and compensation despite higher operational cost, reconsider **Temporal**
- Windmill could be added as a complementary tool for operator-facing administrative workflows
- Monitor SQLite write throughput if concurrent workers are added; this is the primary scaling signal for when a migration becomes necessary

**References**:

- [honker](https://honker.dev/)
- [Procrastinate Documentation](https://procrastinate.readthedocs.io/)
- [absurd documentation](https://earendil-works.github.io/absurd/)
- [absurd GitHub repository](https://github.com/earendil-works/absurd)
- [absurd comparison: PGMQ, Temporal, Inngest, DBOS](https://earendil-works.github.io/absurd/comparison/)
- [Prefect Documentation](https://docs.prefect.io/)
- [Restate Documentation](https://docs.restate.dev/)
- [Temporal Documentation](https://docs.temporal.io/)
- [Windmill Documentation](https://docs.windmill.dev/)
