# Delta for Conversion API

## ADDED Requirements

### Requirement: Reject job submissions when queue depth exceeds configured limit

The system SHALL reject job submissions with HTTP 503 when the number of actionable jobs (status `QUEUED` or `FAILED_RETRYABLE`) meets or exceeds the configured `queue_max_depth`, and SHALL include a `Retry-After` header whose value is the configured `queue_retry_after_seconds` (default: 30).

#### Scenario: Queue at capacity rejects submission

- **GIVEN** the number of jobs with status `QUEUED` or `FAILED_RETRYABLE` is equal to or greater than `queue_max_depth`
- **WHEN** a client submits a new conversion job
- **THEN** the system returns HTTP 503 with a structured error body indicating the queue is full and a `Retry-After` header

**Schema reference:** openapi `POST /v1/jobs` → 503 response with `Retry-After` header

#### Scenario: Queue below capacity accepts submission

- **GIVEN** the number of jobs with status `QUEUED` or `FAILED_RETRYABLE` is below `queue_max_depth`
- **WHEN** a client submits a new conversion job
- **THEN** the system processes the submission normally (existing 201/200 behavior)

#### Scenario: Duplicate submission bypasses queue depth check

- **GIVEN** the queue is at capacity
- **WHEN** a client submits a job whose idempotency key matches an existing job
- **THEN** the system returns HTTP 200 with the existing job record (idempotency takes precedence over backpressure)

### Requirement: Optimize job selection query with composite index

The system SHALL maintain a composite index on `(status, earliest_next_attempt_at, queued_at)` on the conversion jobs table to support efficient job claiming and queue depth counting without full table scans.
