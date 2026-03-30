# Proposal: Queue Backpressure

## Intent

The conversion API defines a `queue_max_depth` configuration field (default: 1000) but never enforces it.
Under sustained load, the job queue grows without bound — clients receive no signal to back off, and the database-as-queue pattern degrades as row count increases.

## Scope

**In scope:**

- Spec amendment to `conversion-api` requiring the API to reject submissions when the queue exceeds the configured depth
- Queue depth enforcement in the `POST /v1/jobs` submission endpoint (HTTP 503)
- Composite index on `(status, earliest_next_attempt_at, queued_at)` to optimize the worker's job selection query
- Alembic migration for the new index

**Out of scope:**

- Litestream write topology documentation (separate concern)
- Replacing SQLite-as-queue with a dedicated queue system (ADR-008 scope)
- Worker-side concurrency changes (already implemented)
- Rate limiting or per-client throttling

## Approach

Add a queue depth check to the job submission endpoint: count jobs with actionable statuses (`QUEUED`, `FAILED_RETRYABLE`) and reject with HTTP 503 + `Retry-After` header when the count meets or exceeds `queue_max_depth`.
The check runs inside the existing submission transaction to avoid TOCTOU races on SQLite.

Add a composite index on `(status, earliest_next_attempt_at, queued_at)` via Alembic migration to cover the worker's `claim_next_job` query, which currently requires scanning the full `status` index and then filtering/sorting on the other columns.

## Schema Impact

The OpenAPI schema gains a new 503 response on `POST /v1/jobs` with a structured error body and `Retry-After` header.
No changes to existing request/response models for successful submissions.
