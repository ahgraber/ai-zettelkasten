# Tasks: Queue Backpressure

## Delta Spec

- [x] Write delta spec for `conversion-api` with backpressure requirement and scenarios

## Migration

- [x] Generate Alembic migration adding composite index `(status, earliest_next_attempt_at, queued_at)` on `conversion_jobs`

## API Implementation

- [x] Add `QueueFullError` response schema to API schemas
- [x] Add queue depth check to `submit_job` — count actionable jobs after idempotency check, raise HTTP 503 with `Retry-After` when at capacity
- [x] Register 503 response in the OpenAPI metadata for `POST /v1/jobs`

## Tests

- [x] Test submission rejected with 503 when queue is at capacity
- [x] Test submission accepted when queue is below capacity
- [x] Test duplicate submission bypasses queue depth check when queue is full
- [x] Test `Retry-After` header is present in 503 response
