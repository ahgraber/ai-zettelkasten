# 009 - Karakeep Webhook Integration

## Status

December 29, 2025 - Proposed

## Context

Karakeep provides user-configurable outbound webhooks that fire on bookmark lifecycle events (created, changed, crawled). Webhook payloads include `jobId`, `type` (e.g., `link`), `bookmarkId`, `userId`, `url`, and `operation`, and are delivered as JSON over HTTP POST. Authentication uses `Authorization: Bearer <WEBHOOK_TOKEN>`. Delivery behavior is governed by `WEBHOOK_TIMEOUT_SEC`, `WEBHOOK_RETRY_TIMES`, and `WEBHOOK_NUM_WORKERS`.

AI Zettelkasten needs to submit ingestion/conversion jobs when new or updated bookmarks arrive. Polling Karakeep's API would add latency and load, while webhooks provide near-real-time triggers with minimal traffic.

## Decision

### Selected Approach

Accept Karakeep webhook callbacks and translate them into internal job submissions. Expose a dedicated HTTP endpoint that validates the bearer token, enforces idempotency using `jobId` + `operation`, and enqueues a downstream conversion/ingestion job for bookmark URLs.

### Rationale

- Push model eliminates polling and reduces latency between bookmark creation and ingestion.
- `jobId` supplied by Karakeep enables idempotent processing and safe retries.
- Simple bearer validation aligns with Karakeep's webhook contract; no custom signing required.
- Fits existing Prefect-based orchestration by handing off to a background queue quickly, keeping the webhook response path fast (< timeout).

### Consequences

#### Positive Impacts

- Faster ingestion after bookmark creation or crawl completion.
- Reduced load versus periodic polling.
- Clear mapping from Karakeep events to internal jobs; easy observability on webhook receipt.

#### Potential Risks

- Replay or spoofed requests if token is leaked.
- Duplicate deliveries from retries if idempotency is not enforced.
- Backpressure if downstream queue is unavailable within webhook timeout.

#### Mitigation Strategies

- Store `WEBHOOK_TOKEN` securely; validate `Authorization` header on every request.
- Persist processed (`jobId`, `operation`) pairs to ignore duplicates; design handlers to be idempotent.
- Acknowledge quickly (HTTP 200) and offload work to a queue/Prefect task; return 503 on persistent enqueue failures to allow Karakeep retries.
- Optional IP allowlist or proxy isolation if deployed publicly.

### Alternatives Considered

#### Option 1: Poll Karakeep API

**Pros**: Simpler auth (API key), no inbound exposure.

**Cons**: Higher latency, unnecessary load, harder deduplication, scheduling overhead.

**Reason not selected**: Webhooks provide lower latency and better fit for event-driven ingestion.

#### Option 2: Manual export/import

**Pros**: No infrastructure change.

**Cons**: Not automated; delays ingestion; operationally brittle.

**Reason not selected**: Fails real-time goal.

## Implementation Details

- **Endpoint**: Add `/webhooks/karakeep` (POST) in the FastAPI service.
- **Auth**: Require `Authorization: Bearer <WEBHOOK_TOKEN>`; configure via environment.
- **Payload handling**:
  - Accept JSON with fields `jobId`, `type`, `bookmarkId`, `userId`, `url`, `operation`.
  - Reject missing required fields with 400; ignore unknown fields.
- **Idempotency**: Persist processed `(jobId, operation)`; skip duplicates.
- **Routing logic**:
  - On `operation` in {`created`, `crawled`}: enqueue ingestion/conversion job for `url` (and associate `bookmarkId`, `userId`).
  - On other operations: log and optionally no-op until mapped.
- **Response policy**: Return 200 once the enqueue succeeds; return 401 on auth failure; 400 on bad payload; 503 on enqueue/storage failure to trigger Karakeep retry.
- **Timeout**: Keep handler under `WEBHOOK_TIMEOUT_SEC` by offloading to background queue/Prefect.
- **Observability**: Log receipt and outcome (including `jobId`, `bookmarkId`, `operation`); add metrics for success/failure/latency.
- **Security**: Run behind TLS; optionally restrict source IPs; avoid logging secrets.

## Related ADRs

- [008-orchestration.md](008-orchestration.md): Webhook handler hands off to Prefect-managed ingestion flows.
- [001-content-archiving.md](001-content-archiving.md): Ingestion triggers downstream archiving and conversion pipelines.

## Additional Notes

- Karakeep webhook docs: https://docs.karakeep.app/configuration/#webhook-configs
- Environment toggles: `WEBHOOK_TIMEOUT_SEC`, `WEBHOOK_RETRY_TIMES`, `WEBHOOK_NUM_WORKERS`, `WEBHOOK_TOKEN`.
