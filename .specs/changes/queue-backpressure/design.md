# Design: Queue Backpressure

## Context

The API uses SQLite as a job queue via polling.
The `submit_job` endpoint creates a `ConversionJob` with status `QUEUED` inside a synchronous SQLAlchemy session.
The worker's `claim_next_job` selects eligible jobs with `BEGIN IMMEDIATE` (exclusive write lock) filtering on `status IN (QUEUED, FAILED_RETRYABLE)`, `earliest_next_attempt_at`, and ordering by `queued_at`.
Only single-column indexes exist today — the worker query cannot be satisfied by a single index scan.

## Decisions

### Decision: Check queue depth after idempotency check

**Chosen:** Run the queue depth `COUNT(*)` only for non-duplicate submissions — after the idempotency key lookup returns no match.

**Rationale:** Idempotent resubmissions are safe regardless of queue depth (they don't add work).
Checking depth first would reject idempotent requests unnecessarily, breaking the contract that duplicate submissions always return the existing job.

**Alternatives considered:**

- Check depth before idempotency: simpler control flow, but rejects safe duplicate submissions when the queue is full — violates the idempotency contract.

### Decision: Count actionable statuses only

**Chosen:** Count jobs with status `QUEUED` or `FAILED_RETRYABLE` for the depth check.
These are the statuses the worker will pick up.

**Rationale:** `RUNNING`, `SUCCEEDED`, `FAILED_PERM`, and `CANCELLED` jobs are not queued work.
Including them would make the depth limit meaningless — a system that has processed 1000 jobs would permanently reject new submissions.

**Alternatives considered:**

- Count all non-terminal statuses (including `RUNNING`): would make the limit sensitive to in-flight work, not just queued depth.
  Rejected because the intent is backpressure on _queued_ work, and `worker_concurrency` already bounds in-flight work.

### Decision: Use `Retry-After` header with configurable value

**Chosen:** Return a `Retry-After` header with the worker poll interval as a reasonable floor, giving clients a concrete signal for when to retry.

**Rationale:** RFC 7231 recommends `Retry-After` with 503 responses.
Using the poll interval aligns with how fast the queue can drain.

**Alternatives considered:**

- No `Retry-After`: simpler, but clients have no guidance on retry timing and may hammer the endpoint.
- Dynamic calculation based on queue drain rate: over-engineered for the current single-worker architecture.

### Decision: Composite index covers worker poll query

**Chosen:** `(status, earliest_next_attempt_at, queued_at)` composite index.

**Rationale:** Matches the worker's `claim_next_job` query exactly — filter on `status`, filter on `earliest_next_attempt_at`, order by `queued_at`.
Also benefits the queue depth `COUNT(*)` which filters on `status` alone (leftmost prefix).
The existing single-column `status` index becomes redundant but is left in place to avoid a migration that drops it (low cost to keep).

**Alternatives considered:**

- `(status, queued_at)` only: doesn't cover the `earliest_next_attempt_at` filter, so the worker query still requires a secondary lookup.

## Architecture

```text
Client POST /v1/jobs
  │
  ▼
submit_job()
  │
  ├─ lookup bookmark (create if missing)
  ├─ compute idempotency key
  ├─ check existing job → 200 (bypass depth check)
  │
  ├─ ** NEW: COUNT(*) WHERE status IN (QUEUED, FAILED_RETRYABLE) **
  │   └─ >= queue_max_depth → 503 + Retry-After
  │
  └─ create job → 201
```

## Risks

- **COUNT(\*) cost under high row count**: mitigated by the composite index (leftmost prefix on `status`) and the fact that actionable statuses are a small fraction of total rows in a healthy system.
- **TOCTOU between count and insert**: on SQLite with synchronous endpoints, the session's implicit transaction serializes reads and writes.
  Not a concern unless the API moves to async or Postgres with higher isolation requirements.
