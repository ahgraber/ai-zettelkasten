# Expected Schema Changes: Queue Backpressure

## OpenAPI

### Added

- `POST /v1/jobs` 503 response: structured error body indicating queue is full, with `Retry-After` header
- Error response schema for queue-full condition (`detail`, `retry_after` fields)
