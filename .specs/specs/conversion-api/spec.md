# Conversion API Specification

> Generated from code analysis on 2026-03-23
> Source files: src/aizk/conversion/api/main.py, src/aizk/conversion/api/routers/jobs.py, src/aizk/conversion/api/routers/bookmarks.py, src/aizk/conversion/api/routers/outputs.py, src/aizk/conversion/api/schemas.py

## Purpose

The Conversion API exposes REST endpoints for submitting, querying, retrying, and cancelling bookmark conversion jobs.
It accepts requests from client applications and enqueues work for the conversion worker without invoking external services during request handling.
It also surfaces conversion outputs and aggregate job status metrics.

## Requirements

### Requirement: Accept job submission without external service calls

The system SHALL accept conversion job submissions via a REST endpoint whose request body types `source_ref` as the narrow `IngressSourceRef` discriminated union (required), and SHALL enqueue the job without invoking any external services during request handling.
At cutover `IngressSourceRef` admits only `KarakeepBookmarkRef`; widening the admitted set is a deployment-config change via `IngressPolicy` and does not alter the internal `SourceRef` contract.
The `karakeep_id` field is removed from the request body; callers SHALL submit `KarakeepBookmarkRef` as the `source_ref` variant instead. (Previously: the endpoint accepted a `karakeep_id` string field.
Now it accepts only `source_ref`.)

The API SHALL materialize Source identity at submit time: parse `source_ref` against `IngressSourceRef`, gate its `kind` against `SubmissionCapabilities.accepted_submission_kinds`, canonicalize via the variant's `to_dedup_payload()`, compute `source_ref_hash`, create or reuse a Source row keyed on the hash, and persist the job with the resulting `aizk_uuid` FK.
The stored `Source.source_ref` and the denormalized `Job.source_ref` retain the wide `SourceRef` type so that future widening of `IngressPolicy` does not require a schema change.
Source reuse under concurrent submission SHALL use `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` followed by `SELECT` on the hash so that two simultaneous submissions of the same `source_ref` share a single Source row; distinct jobs MAY still be created and are deduplicated at the job level by `idempotency_key`.
For `KarakeepBookmarkRef` submissions, the API SHALL populate `Source.karakeep_id` from `bookmark_id`; for all other variants (once admitted by `IngressPolicy`), `karakeep_id` SHALL be null.
The API SHALL compute the idempotency key at submit time, including `source_ref_hash`, `converter_name`, and the converter's output-affecting config snapshot in the hash, so that jobs for different source refs, different converters, or different converter configurations produce distinct keys.
This idempotency formula replaces the pre-refactor formula (which hashed `aizk_uuid` and Docling-specific fields).
Replay-idempotency across the cutover is preserved by the `bookmarks → sources` migration using a canonical-job strategy: for each source, the migration designates the most-recent SUCCEEDED job (falling back to the most-recent job by id) as the canonical historical job and rewrites its `idempotency_key` to `sha256("{source_ref_hash}:docling:{frozen_config_json}")`, which matches `compute_idempotency_key()`. Accordingly, a post-migration re-submission of the same source with default config SHALL hit the canonical historical job and not produce a new job. Additional historical jobs for the same source (i.e., when multiple jobs existed pre-migration) receive a job-id-suffixed key that is stable and unique but not reachable by re-submission. Historical jobs originally submitted with non-default config cannot be restored to continuity — the original config is not recoverable at migration time — so re-submissions with non-default config will still create new jobs regardless.
Source identity columns (`aizk_uuid`, `source_ref`, `source_ref_hash`, `karakeep_id`) SHALL be immutable after creation; worker writes are confined to mutable metadata columns.

**Schema reference:** `POST /v1/jobs` · request: `JobSubmission` (updated) · response: `JobResponse` (updated)

#### Scenario: Submit with source_ref

- **GIVEN** a valid `IngressSourceRef` (at cutover: `KarakeepBookmarkRef`) whose `kind` is in `SubmissionCapabilities.accepted_submission_kinds`
- **WHEN** a client submits a conversion job
- **THEN** the job is created with the provided `source_ref` and linked to a Source row

#### Scenario: Concurrent submissions of the same source_ref share one Source row

- **GIVEN** two clients simultaneously submit jobs with `source_ref` values that canonicalize to the same `source_ref_hash`
- **WHEN** both requests race through Source materialization
- **THEN** exactly one Source row exists for that hash, both jobs reference its `aizk_uuid`, and job-level deduplication proceeds via `idempotency_key`

#### Scenario: Missing source_ref returns 422

- **GIVEN** a submission with no `source_ref` field
- **WHEN** the API validates the request
- **THEN** HTTP 422 is returned

### Requirement: Gate accepted source ref kinds via SubmissionCapabilities

The API SHALL validate `source_ref.kind` in two layers and SHALL reject non-admissible submissions with HTTP 422.

1. **Schema layer.**
   The request body types `source_ref` as the narrow `IngressSourceRef` union, so pydantic parsing rejects any `kind` outside that union before application code sees the request.
2. **Policy layer.**
   After parsing, the API SHALL gate `source_ref.kind` against `SubmissionCapabilities.accepted_submission_kinds`, which is sourced from deployment-level `IngressPolicy` — not from fetcher-registry membership.
   A kind that is registered for worker dispatch but excluded from `IngressPolicy` SHALL be rejected.

The subset invariant `accepted_submission_kinds ⊆ DeploymentCapabilities.registered_kinds` is enforced at API startup (see the `pluggable-pipeline` delta); the API layer relies on that invariant and does not re-check registry membership at request time.

#### Scenario: Admitted kind accepted

- **GIVEN** `SubmissionCapabilities.accepted_submission_kinds = {"karakeep_bookmark"}` because `IngressPolicy` admits that kind
- **WHEN** a client submits a job with `source_ref.kind = "karakeep_bookmark"`
- **THEN** the submission is accepted

#### Scenario: Kind outside IngressSourceRef rejected at schema layer

- **GIVEN** `IngressSourceRef` admits only `KarakeepBookmarkRef` at cutover
- **WHEN** a client submits a job with `source_ref.kind = "single_file"` (a kind not present in `IngressSourceRef`)
- **THEN** HTTP 422 is returned by pydantic parsing before the policy layer runs

#### Scenario: Registered-but-not-admitted kind rejected at policy layer

- **GIVEN** `DeploymentCapabilities.registered_kinds` includes `"url"` (worker can dispatch it) but `IngressPolicy` does not admit `"url"`, so `SubmissionCapabilities.accepted_submission_kinds` excludes it
- **WHEN** a client submits a job with `source_ref.kind = "url"`
- **THEN** HTTP 422 is returned with an error indicating the kind is not admitted for submission in this deployment

### Requirement: Reject duplicate job submissions

The system SHALL reject job submissions whose computed idempotency key matches an existing job record.

#### Scenario: Duplicate idempotency key rejected

- **GIVEN** a bookmark with an identical idempotency key already exists
- **WHEN** a client resubmits the same bookmark
- **THEN** the system returns the existing job details without creating a new record

### Requirement: Retrieve individual job status

The system SHALL include the `source_ref` in the job response as the canonical source identifier.
`karakeep_id` SHALL be retained on `JobResponse` as a nullable compatibility field — populated when `source_ref.kind == "karakeep_bookmark"`, null otherwise — so existing UI consumers continue to function without a parallel UI migration.
Existing fields `url: AnyUrl | None` and `title: str | None` SHALL retain their current names and semantics (populated for sources that have been enriched with a URL or title; null otherwise). (Previously: response always included a non-null top-level `karakeep_id`.
Now `karakeep_id` is nullable, and `source_ref` is added alongside it.)

**Schema reference:** `GET /v1/jobs/{job_id}` · response: `JobResponse` (updated)

#### Scenario: KaraKeep job response includes source_ref and karakeep_id

- **GIVEN** a job sourced from a KaraKeep bookmark
- **WHEN** the job is retrieved
- **THEN** the response includes `source_ref` with kind `"karakeep_bookmark"`, `karakeep_id` is populated with the bookmark id, and `url` and `title` are populated when available

#### Scenario: Non-KaraKeep job response has null karakeep_id

- **GIVEN** a job sourced from a `UrlRef`
- **WHEN** the job is retrieved
- **THEN** the response includes `source_ref` with kind `"url"`, and `karakeep_id` is null

### Requirement: List jobs with filters and pagination

The system SHALL expose an endpoint to list conversion jobs filterable by status, internal source identifier, and supporting pagination.

**Schema reference:** `GET /v1/jobs` · query params: status, aizk_uuid, created_after, created_before, limit (1–1000, default 50), offset (≥0, default 0) · response: `JobList`

#### Scenario: Filter jobs by status

- **GIVEN** jobs exist with multiple statuses
- **WHEN** a client requests jobs filtered by a specific status
- **THEN** only jobs matching that status are returned

#### Scenario: Filter jobs by identifier

- **GIVEN** jobs exist for multiple bookmarks
- **WHEN** a client filters by internal bookmark identifier or KaraKeep identifier
- **THEN** only matching jobs are returned with pagination applied

### Requirement: Return aggregate job status counts

The system SHALL expose an endpoint returning the count of jobs grouped by status.

**Schema reference:** `GET /v1/jobs/status-counts` · response: `JobStatusCounts`

#### Scenario: Status counts returned

- **GIVEN** jobs exist in various statuses
- **WHEN** a client requests the status counts endpoint
- **THEN** the response returns a count for each status value present in the system

### Requirement: Retry failed jobs

The system SHALL expose an endpoint to retry a failed or permanently failed job by resetting its status to QUEUED and incrementing its attempt count.

**Schema reference:** `POST /v1/jobs/{job_id}/retry` · response: `JobResponse`

#### Scenario: Retry a failed-retryable job

- **GIVEN** a job has status FAILED_RETRYABLE or FAILED_PERM
- **WHEN** a client posts a retry request for that job
- **THEN** the job status resets to QUEUED, the attempt count increments by one, and the retry scheduling timestamp is cleared

### Requirement: Cancel jobs

The system SHALL expose an endpoint to cancel queued or running jobs on a best-effort basis.

**Schema reference:** `POST /v1/jobs/{job_id}/cancel` · response: `JobResponse`

#### Scenario: Cancel a queued job

- **GIVEN** a job has status QUEUED
- **WHEN** a client posts a cancel request for that job
- **THEN** the job transitions to CANCELLED and will not be processed

#### Scenario: Cancel a running job

- **GIVEN** a job has status RUNNING
- **WHEN** a client posts a cancel request for that job
- **THEN** the system attempts best-effort cancellation and updates the job status to CANCELLED

### Requirement: Apply bulk actions across multiple jobs

The system SHALL expose an endpoint accepting a list of job identifiers and a bulk action (retry or cancel) to apply to all specified jobs, accepting between 1 and 100 job identifiers.

**Schema reference:** `POST /v1/jobs/actions` · request: `BulkJobActionRequest` · response: `BulkActionResponse`

#### Scenario: Bulk retry

- **GIVEN** multiple failed jobs are selected
- **WHEN** a client posts a bulk retry action with their identifiers
- **THEN** all eligible jobs are reset to QUEUED and a result summary is returned

#### Scenario: Bulk cancel

- **GIVEN** multiple queued or running jobs are selected
- **WHEN** a client posts a bulk cancel action with their identifiers
- **THEN** all eligible jobs are transitioned to CANCELLED and a result summary is returned

### Requirement: Retrieve conversion outputs for a bookmark

The system SHALL expose an endpoint returning all conversion output records for a bookmark ordered by creation time descending, with an option to return only the most recent output.

**Schema reference:** `GET /v1/bookmarks/{aizk_uuid}/outputs` · query param: latest (bool, default false) · response: list of `OutputResponse`

#### Scenario: Retrieve all outputs

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs for the bookmark's internal identifier
- **THEN** all conversion output records are returned ordered by creation time descending

#### Scenario: Retrieve latest output only

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs with the latest flag set
- **THEN** only the most recently created conversion output record is returned

### Requirement: Serve raw manifest JSON for a conversion output

The system SHALL expose an endpoint that retrieves and returns the raw manifest JSON for a conversion output record directly from object storage without re-parsing or transforming the content.

**Schema reference:** `GET /v1/outputs/{output_id}/manifest` · response: `application/json` raw bytes

#### Scenario: Retrieve manifest for a known output

- **GIVEN** a conversion output record exists with a valid manifest key
- **WHEN** a client requests the manifest by output identifier
- **THEN** the system returns the raw manifest bytes with Content-Type `application/json`

#### Scenario: Manifest object missing from storage

- **GIVEN** a conversion output record exists but its manifest object is absent from storage
- **WHEN** a client requests the manifest
- **THEN** the system returns a 404 response

### Requirement: Serve markdown content for a conversion output

The system SHALL expose an endpoint that retrieves and returns the converted markdown text for a conversion output record directly from object storage.

**Schema reference:** `GET /v1/outputs/{output_id}/markdown` · response: `text/markdown; charset=utf-8` raw bytes

#### Scenario: Retrieve markdown for a known output

- **GIVEN** a conversion output record exists with a valid markdown key
- **WHEN** a client requests the markdown by output identifier
- **THEN** the system returns the markdown bytes with Content-Type `text/markdown; charset=utf-8`

### Requirement: Serve figure images for a conversion output

The system SHALL expose an endpoint that retrieves and returns individual figure images for a conversion output record by filename, and SHALL reject filenames that could escape the figures storage prefix.

**Schema reference:** `GET /v1/outputs/{output_id}/figures/{filename}`

#### Scenario: Retrieve a valid figure

- **GIVEN** a conversion output record exists and a figure with the requested filename is present in object storage
- **WHEN** a client requests the figure by output identifier and bare filename
- **THEN** the system returns the figure bytes with an appropriate image Content-Type

#### Scenario: Reject path-traversal filename

- **GIVEN** a client submits a filename containing `/` or an empty filename
- **WHEN** the API receives the request
- **THEN** the system returns a 4xx error response without accessing object storage

#### Scenario: Output has no figures

- **GIVEN** a conversion output record with `figure_count == 0`
- **WHEN** a client requests any figure by filename
- **THEN** the system returns a 404 response

### Requirement: Return structured error responses for storage failures

The system SHALL return a 502 response when object storage returns an unexpected error, and a 404 response when the requested object key does not exist.

#### Scenario: Object storage error on content fetch

- **GIVEN** object storage returns an error other than key-not-found
- **WHEN** a client requests any content endpoint
- **THEN** the system returns a 502 response

### Requirement: Expose liveness probe

The system SHALL expose a liveness endpoint that returns HTTP 200 when the API process is running and responsive, without checking any external dependencies.

**Schema reference:** `GET /health/live` · response: `HealthResponse`

#### Scenario: Liveness check succeeds

- **GIVEN** the API process is running
- **WHEN** a client requests the liveness endpoint
- **THEN** the system returns HTTP 200 with status "ok"

### Requirement: Expose readiness probe with dependency checks

The system SHALL expose a readiness endpoint that reports whether each required external dependency (database, S3, and — when configured — the picture description endpoint) is reachable, returning HTTP 200 when all checks pass and HTTP 503 when any check fails.
When the picture description endpoint is not configured, it SHALL be omitted from the check results entirely.

**Schema reference:** `GET /health/ready` · response: `HealthResponse`

#### Scenario: All dependencies healthy

- **GIVEN** the database is reachable and S3 credentials are valid
- **WHEN** a client requests the readiness endpoint
- **THEN** the system returns HTTP 200 with status "ok" and individual check results showing each dependency as healthy

#### Scenario: Database unreachable

- **GIVEN** the database connection fails or times out
- **WHEN** a client requests the readiness endpoint
- **THEN** the system returns HTTP 503 with status "unavailable" and the database check result includes the failure reason

#### Scenario: S3 unreachable

- **GIVEN** S3 returns an error or times out on a HEAD bucket request
- **WHEN** a client requests the readiness endpoint
- **THEN** the system returns HTTP 503 with status "unavailable" and the S3 check result includes the failure reason

#### Scenario: Multiple dependencies unhealthy

- **GIVEN** both the database and S3 are unreachable
- **WHEN** a client requests the readiness endpoint
- **THEN** the system returns HTTP 503 with all failing check results reported — checks are not short-circuited

#### Scenario: Picture description endpoint included when configured

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` and `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY` are set
- **WHEN** a client requests `/health/ready`
- **THEN** the response includes a `picture_description` check result alongside `database` and `s3`

#### Scenario: Picture description check fails after startup

- **GIVEN** the picture description endpoint was reachable at startup but is now unreachable
- **WHEN** a client requests `/health/ready`
- **THEN** the `picture_description` check result has status `"unavailable"`, the overall response status is `"unavailable"`, and the HTTP status is 503

#### Scenario: Picture description omitted when not configured

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` is not set
- **WHEN** a client requests `/health/ready`
- **THEN** the response contains only `database` and `s3` check results, with no `picture_description` entry

### Requirement: Bound readiness check duration

The system SHALL enforce a per-check timeout on each readiness dependency check to prevent a slow or unresponsive dependency from hanging the probe response.

#### Scenario: Dependency check exceeds timeout

- **GIVEN** a dependency check does not complete within its timeout
- **WHEN** the readiness endpoint is evaluating checks
- **THEN** the timed-out check is reported as unhealthy with a timeout indication and the overall response is HTTP 503

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

## Technical Notes

- **Implementation**: `src/aizk/conversion/api/`
- **Routes**:
  - `POST /v1/jobs` — submit job
  - `GET /v1/jobs` — list jobs (filters: status, aizk_uuid, created_after, created_before; pagination: limit, offset)
  - `GET /v1/jobs/status-counts` — aggregate counts by status
  - `GET /v1/jobs/{job_id}` — get single job
  - `POST /v1/jobs/{job_id}/retry` — retry failed/cancelled job
  - `POST /v1/jobs/{job_id}/cancel` — cancel queued/running job
  - `POST /v1/jobs/actions` — bulk retry or cancel (1–100 job IDs)
  - `GET /v1/bookmarks/{aizk_uuid}/outputs` — list conversion outputs for a bookmark
  - `GET /v1/outputs/{output_id}/manifest` — raw manifest JSON from S3
  - `GET /v1/outputs/{output_id}/markdown` — markdown text from S3
  - `GET /v1/outputs/{output_id}/figures/{filename}` — figure image from S3
  - `GET /health/live` — liveness probe (no dependency checks)
  - `GET /health/ready` — readiness probe (DB + S3 + optional picture description health checks)
- **Dependencies**: conversion-worker (data model); conversion-ui (served under same process)
- **Idempotency key**: computed by the worker as a hash of bookmark identifier, payload version, Docling version, and config hash; the API surface accepts a client-supplied key as an override
- **Readiness probe shape**: database check via a short-lived connection; S3 check via HEAD bucket; picture description check issues `GET {base_url}/models` with an `Authorization: Bearer` header and a 5-second per-check timeout
- **Indexes**: composite index on `(status, earliest_next_attempt_at, queued_at)` on the conversion jobs table supports job claiming and queue-depth counting without full table scans
