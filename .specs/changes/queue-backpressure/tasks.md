# Tasks: Queue Backpressure

## Delta Spec

- [x] Write delta spec for `conversion-api` with backpressure requirement and scenarios

## Migration

- [ ] Generate Alembic migration adding composite index `(status, earliest_next_attempt_at, queued_at)` on `conversion_jobs`

## API Implementation

- [ ] Add `QueueFullError` response schema to API schemas
- [ ] Add queue depth check to `submit_job` — count actionable jobs after idempotency check, raise HTTP 503 with `Retry-After` when at capacity
- [ ] Register 503 response in the OpenAPI metadata for `POST /v1/jobs`

## Tests

- [ ] Test submission rejected with 503 when queue is at capacity
- [ ] Test submission accepted when queue is below capacity
- [ ] Test duplicate submission bypasses queue depth check when queue is full
- [ ] Test `Retry-After` header is present in 503 response
