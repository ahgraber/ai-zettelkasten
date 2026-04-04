# 008 - Workflow Orchestration

## Status

- December 23, 2024 - Accepted (initial)
- April 4, 2026 - Revised: SQLite task queue adopted; Prefect not pursued

## Context

The AI Zettelkasten system requires orchestration for managing complex workflows including:

- Document ingestion and conversion pipelines
- Long-running parsing and extraction tasks
- Scheduled reprocessing and batch operations
- Retry logic for failed operations
- Coordination between multiple processing stages

The project is self-hosted, personal/internal in scope, and requires a solution that balances operational complexity with workflow durability.
Three self-hosted orchestration platforms were evaluated in the original decision: Temporal.io, Prefect.io, and Windmill.dev.

### Revision Context (April 2026)

After selecting Prefect, practical implementation revealed that the project's actual orchestration needs are well within the scope of a simple SQLite-backed job queue.
The `ConversionJob` table (in the application's existing SQLite database) already provides queuing, retry tracking, exponential backoff, idempotency keys, and stale job recovery with no additional infrastructure.
Adding Prefect would introduce operational overhead — a separate server process, its own backend database, worker pool management — without a clear benefit at this scale.

## Decision

### Selected Approach

**SQLite-backed task queue** using the application's existing `ConversionJob` table is sufficient for current needs.
No external orchestration system is adopted at this time.

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

#### Mitigation Strategies

- **Design for idempotency**: all tasks are safe to retry from the beginning
- **Service boundary preservation**: orchestration logic stays in `workers/loop.py` and `workers/orchestrator.py`, decoupled from business logic
- **Migration path**: job model is intentionally simple — a Postgres migration is straightforward (see Future Considerations)

### Alternatives Considered

#### Option 1: absurd _(preferred if migrating to Postgres)_

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

**When to select**: If the project migrates to Postgres for any reason, absurd is the preferred next step before considering Prefect or Temporal.
It extends the "Postgres as infrastructure" philosophy and avoids introducing a separate orchestration server.
Installation is `uv add absurd-sdk`; schema setup is `uvx absurdctl init -d <database>`.

#### Option 2: Prefect.io _(original selection — not pursued)_

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

#### Option 3: Temporal.io

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

#### Option 4: Windmill.dev

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

**Current Setup** (SQLite task queue):

- `ConversionJob` table in the application SQLite database (via SQLModel/SQLAlchemy)
- `workers/loop.py`: polling loop, stale job recovery, thread pool
- `workers/orchestrator.py`: per-job execution lifecycle and error classification
- Litestream provides continuous replication for durability

**If migrating to absurd**:

1. Migrate application database to Postgres
2. Apply absurd schema: `uvx absurdctl init -d <database>`
3. Replace `workers/loop.py` polling with an `absurd-sdk` worker (`uv add absurd-sdk`)
4. Register existing job handler logic as an Absurd task with explicit step boundaries
5. Remove `ConversionJob` table and status-machine logic (replaced by Absurd's model)

**Migration Considerations**:

- Keep orchestration logic separate from core business logic (already the case)
- Maintain clean service boundaries
- Design tasks to be orchestration-agnostic where possible

## Related ADRs

- [002-content-parsing.md](002-content-parsing.md): Parsing workflows are orchestrated by this queue
- [001-content-archiving.md](001-content-archiving.md): Archiving workflows use this queue for retries
- [003-database.md](003-database.md): Application database and Litestream replication strategy

## Additional Notes

**Future Considerations**:

- If workflow complexity increases significantly (step-level checkpointing, event-await semantics, fan-out), migrate to **absurd** on Postgres before reaching for Prefect or Temporal
- Windmill could be added as a complementary tool for operator-facing administrative workflows
- Monitor SQLite write throughput if concurrent workers are added; this is the primary scaling signal for when a migration becomes necessary

**References**:

- [absurd documentation](https://earendil-works.github.io/absurd/)
- [absurd GitHub repository](https://github.com/earendil-works/absurd)
- [absurd comparison: PGMQ, Temporal, Inngest, DBOS](https://earendil-works.github.io/absurd/comparison/)
- [Prefect Documentation](https://docs.prefect.io/)
- [Temporal Documentation](https://docs.temporal.io/)
- [Windmill Documentation](https://docs.windmill.dev/)
