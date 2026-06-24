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
Source reuse under concurrent submission SHALL use `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` followed by `SELECT` on the hash so that two simultaneous submissions of the same `source_ref` share a single Source row; distinct jobs MAY still be created and are deduplicated at the job level by `(owner_id, idempotency_key)`.
Job-level deduplication SHALL be owner-scoped: duplicate detection SHALL match an existing job only when both `idempotency_key` and `owner_id` match the resolved principal.
The API SHALL treat `(principal.subject, idempotency_key)` as the duplicate-submission identity, while preserving the existing key material (`source_ref_hash`, converter name, output-affecting config snapshot).
Two principals submitting the same `source_ref_hash` MAY still share a single Source row; that shared Source row SHALL NOT cause the second principal to reuse the first principal's Job row.
For `KarakeepBookmarkRef` submissions, the API SHALL populate `Source.karakeep_id` from `bookmark_id`; for all other variants (once admitted by `IngressPolicy`), `karakeep_id` SHALL be null.
The API SHALL compute the idempotency key at submit time, including `source_ref_hash`, `converter_name`, and the converter's output-affecting config snapshot in the hash, so that jobs for different source refs, different converters, or different converter configurations produce distinct keys.
This idempotency formula replaces the pre-refactor formula (which hashed `aizk_uuid` and Docling-specific fields).
Replay-idempotency across the cutover is preserved by the `bookmarks → sources` migration using a canonical-job strategy: for each source, the migration designates the most-recent SUCCEEDED job (falling back to the most-recent job by id) as the canonical historical job and rewrites its `idempotency_key` to `sha256("{source_ref_hash}:docling:{frozen_config_json}")`, which matches `compute_idempotency_key()`.
Accordingly, a post-migration re-submission of the same source with default config SHALL hit the canonical historical job and not produce a new job.
Additional historical jobs for the same source (i.e., when multiple jobs existed pre-migration) receive a job-id-suffixed key that is stable and unique but not reachable by re-submission.
Historical jobs originally submitted with non-default config cannot be restored to continuity — the original config is not recoverable at migration time — so re-submissions with non-default config will still create new jobs regardless.
Source identity columns (`aizk_uuid`, `source_ref`, `source_ref_hash`, `karakeep_id`) SHALL be immutable after creation; worker writes are confined to mutable metadata columns.

The API SHALL also persist `owner_id = principal.subject` on every Source row created or reused at submit time and on every Job row created.
The Principal is the value resolved by the dependency described in "Resolve a Principal on every API request".
For Source reuse under concurrent submission (the existing `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` path), the `owner_id` of the winning insert SHALL be the principal of the request that won the race; subsequent submissions for the same `source_ref_hash` SHALL NOT overwrite the existing Source row's `owner_id`.
This means: at cutover, with single-principal `trust_network`, every Source row's `owner_id` is `AIZK_DEFAULT_PRINCIPAL`; in a future multi-principal deployment, the first submitter "owns" the Source and subsequent submitters can still create their own Jobs against it but do not change Source ownership.
The `owner_id` column is internal-only and SHALL NOT appear in the request or response schemas.

**Schema reference:** `POST /v1/jobs` · request: `JobSubmission` (updated) · response: `JobResponse` (updated)

#### Scenario: Submit with source_ref

- **GIVEN** a valid `IngressSourceRef` (at cutover: `KarakeepBookmarkRef`) whose `kind` is in `SubmissionCapabilities.accepted_submission_kinds`
- **WHEN** a client submits a conversion job
- **THEN** the job is created with the provided `source_ref` and linked to a Source row

#### Scenario: Concurrent submissions of the same source_ref share one Source row

- **GIVEN** two clients simultaneously submit jobs with `source_ref` values that canonicalize to the same `source_ref_hash`
- **WHEN** both requests race through Source materialization
- **THEN** exactly one Source row exists for that hash, both jobs reference its `aizk_uuid`, and job-level deduplication proceeds via `idempotency_key`

#### Scenario: Different owners share a Source but not a Job

- **GIVEN** principal A has already submitted a source/config pair, producing a Source row with hash `H` and a Job whose `idempotency_key = K`
- **AND** principal B submits the same source/config pair, so the computed `source_ref_hash = H` and `idempotency_key = K`
- **WHEN** the API handles principal B's submission
- **THEN** the existing Source row MAY be reused, but duplicate detection does not match principal A's Job, and a new Job owned by principal B is created

#### Scenario: Missing source_ref returns 422

- **GIVEN** a submission with no `source_ref` field
- **WHEN** the API validates the request
- **THEN** HTTP 422 is returned

#### Scenario: owner_id recorded on Source and Job at submit

- **GIVEN** `AIZK_AUTH_MODE=trust_network`, `AIZK_DEFAULT_PRINCIPAL=local`, and a valid `KarakeepBookmarkRef` submission
- **WHEN** the API materializes Source identity and creates the Job
- **THEN** the new `sources` row has `owner_id = "self"` and the new `conversion_jobs` row has `owner_id = "self"`

#### Scenario: Source reuse preserves original owner_id

- **GIVEN** an existing `sources` row with `owner_id = "self"` and `source_ref_hash = H`, and a new submission whose `source_ref` canonicalizes to the same hash
- **WHEN** the API materializes Source identity (the `INSERT ... ON CONFLICT DO NOTHING` resolves to reuse)
- **THEN** the existing Source row's `owner_id` is unchanged; only the new Job row is created, with its own `owner_id` from the current Principal

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

The system SHALL return an existing job only when the computed idempotency key matches a job whose `owner_id` also matches `principal.subject`.
A job with the same `idempotency_key` but a different `owner_id` SHALL NOT satisfy the duplicate-submission path.

#### Scenario: Same-owner duplicate returns the existing job

- **GIVEN** principal A has an existing job whose `idempotency_key = K`
- **WHEN** principal A resubmits the same source/config and the API computes `idempotency_key = K`
- **THEN** the system returns the existing job details without creating a new record

#### Scenario: Cross-owner duplicate key does not return another owner's job

- **GIVEN** principal A has an existing job whose `idempotency_key = K`
- **WHEN** principal B submits a source/config pair that computes the same `idempotency_key = K`
- **THEN** the system does not return principal A's job and proceeds as a new submission for principal B

### Requirement: Retrieve individual job status

The system SHALL include the `source_ref` in the job response as the canonical source identifier.
`karakeep_id` SHALL be retained on `JobResponse` as a nullable compatibility field — populated when `source_ref.kind == "karakeep_bookmark"`, null otherwise — so existing UI consumers continue to function without a parallel UI migration.
Existing fields `url: AnyUrl | None` and `title: str | None` SHALL retain their current names and semantics (populated for sources that have been enriched with a URL or title; null otherwise). (Previously: response always included a non-null top-level `karakeep_id`.
Now `karakeep_id` is nullable, and `source_ref` is added alongside it.)

The system SHALL return 404 when the resolved `principal.subject` does not match the job's `owner_id`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected into the handler on every request.
The response body and status code SHALL be identical to the not-found case so that cross-owner access does not leak job existence. (Previously: any job retrievable by id regardless of `owner_id`; `get_principal` not injected.)

**Schema reference:** `GET /v1/jobs/{job_id}` · response: `JobResponse` (updated)

#### Scenario: KaraKeep job response includes source_ref and karakeep_id

- **GIVEN** a job sourced from a KaraKeep bookmark
- **WHEN** the job is retrieved
- **THEN** the response includes `source_ref` with kind `"karakeep_bookmark"`, `karakeep_id` is populated with the bookmark id, and `url` and `title` are populated when available

#### Scenario: Non-KaraKeep job response has null karakeep_id

- **GIVEN** a job sourced from a `UrlRef`
- **WHEN** the job is retrieved
- **THEN** the response includes `source_ref` with kind `"url"`, and `karakeep_id` is null

#### Scenario: Cross-owner get returns 404

- **GIVEN** a job exists with `owner_id != principal.subject`
- **WHEN** a client calls `GET /v1/jobs/{job_id}` for that job
- **THEN** the system returns HTTP 404 with `error: job_not_found`; the response is indistinguishable from a request for a non-existent job id

### Requirement: List jobs with filters and pagination

The system SHALL expose an endpoint to list conversion jobs filterable by status, internal source identifier, and supporting pagination.
The system SHALL filter the job list to jobs whose `owner_id` matches `principal.subject`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected into the handler on every request. (Previously: all jobs returned regardless of `owner_id`; `get_principal` not injected.)

**Schema reference:** `GET /v1/jobs` · query params: status, aizk_uuid, created_after, created_before, limit (1–1000, default 50), offset (≥0, default 0) · response: `JobList`

#### Scenario: Filter jobs by status

- **GIVEN** jobs exist with multiple statuses
- **WHEN** a client requests jobs filtered by a specific status
- **THEN** only jobs matching that status are returned

#### Scenario: Filter jobs by identifier

- **GIVEN** jobs exist for multiple bookmarks
- **WHEN** a client filters by internal bookmark identifier or KaraKeep identifier
- **THEN** only matching jobs are returned with pagination applied

#### Scenario: List returns only caller-owned jobs

- **GIVEN** jobs exist with two distinct `owner_id` values and `AIZK_AUTH_MODE=trust_network`
- **WHEN** a client calls `GET /v1/jobs`
- **THEN** only jobs whose `owner_id` matches `principal.subject` are returned; jobs owned by other principals are absent from the result and the `total` count

#### Scenario: trust_network list is unchanged

- **GIVEN** `AIZK_AUTH_MODE=trust_network` and all jobs share `owner_id = AIZK_DEFAULT_PRINCIPAL`
- **WHEN** a client calls `GET /v1/jobs`
- **THEN** the response is identical to the pre-change behavior (all jobs visible, filter is a no-op because every job is owned by the single principal)

### Requirement: Return aggregate job status counts

The system SHALL expose an endpoint returning the count of jobs grouped by status.
The system SHALL count only jobs whose `owner_id` matches `principal.subject`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected into the handler on every request. (Previously: counts are global across all jobs; `get_principal` not injected.)

**Schema reference:** `GET /v1/jobs/status-counts` · response: `JobStatusCounts`

#### Scenario: Status counts returned

- **GIVEN** jobs exist in various statuses
- **WHEN** a client requests the status counts endpoint
- **THEN** the response returns a count for each status value present in the system

#### Scenario: Status counts are owner-scoped

- **GIVEN** jobs exist for two distinct owners and `AIZK_AUTH_MODE` admits multiple principals
- **WHEN** each principal calls `GET /v1/jobs/status-counts`
- **THEN** each response reflects only that principal's jobs; the two responses need not sum to the global total

#### Scenario: trust_network counts are unchanged

- **GIVEN** `AIZK_AUTH_MODE=trust_network`
- **WHEN** a client calls `GET /v1/jobs/status-counts`
- **THEN** the response is identical to the pre-change behavior (all jobs counted, filter is a no-op)

### Requirement: Retry failed jobs

The system SHALL expose an endpoint to retry a failed or permanently failed job by resetting its status to QUEUED and incrementing its attempt count.
The system SHALL return 404 when the resolved `principal.subject` does not match the target job's `owner_id`, using the same response body as the not-found case. (Previously: `principal` injected but not used for authorization enforcement.)

**Schema reference:** `POST /v1/jobs/{job_id}/retry` · response: `JobResponse`

#### Scenario: Retry a failed-retryable job

- **GIVEN** a job has status FAILED_RETRYABLE or FAILED_PERM
- **WHEN** a client posts a retry request for that job
- **THEN** the job status resets to QUEUED, the attempt count increments by one, and the retry scheduling timestamp is cleared

#### Scenario: Cross-owner retry returns 404

- **GIVEN** a job with `owner_id != principal.subject`
- **WHEN** a client posts `POST /v1/jobs/{job_id}/retry`
- **THEN** the system returns HTTP 404 with `error: job_not_found`

### Requirement: Cancel jobs

The system SHALL expose an endpoint to cancel queued or running jobs on a best-effort basis.
The system SHALL return 404 when the resolved `principal.subject` does not match the target job's `owner_id`, using the same response body as the not-found case. (Previously: `principal` injected but not used for authorization enforcement.)

**Schema reference:** `POST /v1/jobs/{job_id}/cancel` · response: `JobResponse`

#### Scenario: Cancel a queued job

- **GIVEN** a job has status QUEUED
- **WHEN** a client posts a cancel request for that job
- **THEN** the job transitions to CANCELLED and will not be processed

#### Scenario: Cancel a running job

- **GIVEN** a job has status RUNNING
- **WHEN** a client posts a cancel request for that job
- **THEN** the system attempts best-effort cancellation and updates the job status to CANCELLED

#### Scenario: Cross-owner cancel returns 404

- **GIVEN** a job with `owner_id != principal.subject`
- **WHEN** a client posts `POST /v1/jobs/{job_id}/cancel`
- **THEN** the system returns HTTP 404 with `error: job_not_found`

### Requirement: Apply bulk actions across multiple jobs

The system SHALL expose an endpoint accepting a list of job identifiers and a bulk action (retry or cancel) to apply to all specified jobs, accepting between 1 and 100 job identifiers.
The system SHALL treat each job in the request independently: a job whose `owner_id` does not match `principal.subject` SHALL be returned with `status: "error"` and `error: "job_not_found"` in the per-job result, consistent with the existing not-found error path and the 404-posture for cross-owner access.
The presence of cross-owner job ids in the request SHALL NOT cause the entire bulk operation to fail; eligible owned jobs SHALL still be actioned. (Previously: `principal` injected but not used for authorization enforcement.)

**Schema reference:** `POST /v1/jobs/actions` · request: `BulkJobActionRequest` · response: `BulkActionResponse`

#### Scenario: Bulk retry

- **GIVEN** multiple failed jobs are selected
- **WHEN** a client posts a bulk retry action with their identifiers
- **THEN** all eligible jobs are reset to QUEUED and a result summary is returned

#### Scenario: Bulk cancel

- **GIVEN** multiple queued or running jobs are selected
- **WHEN** a client posts a bulk cancel action with their identifiers
- **THEN** all eligible jobs are transitioned to CANCELLED and a result summary is returned

#### Scenario: Bulk action skips cross-owner jobs

- **GIVEN** a bulk request containing one job owned by the caller and one job owned by a different principal
- **WHEN** a client posts `POST /v1/jobs/actions`
- **THEN** the owned job is actioned and returned with `status: "success"`; the cross-owner job is returned with `status: "error"` and `error: "job_not_found"`; the summary `errors` count includes the cross-owner job

### Requirement: Retrieve conversion outputs for a bookmark

The system SHALL expose an endpoint returning all conversion output records for a bookmark ordered by creation time descending, with an option to return only the most recent output.
The system SHALL scope bookmark-output listing to `ConversionOutput.owner_id`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected into the handler on every request.
The response SHALL include only output rows whose `aizk_uuid` matches the requested bookmark identifier **and** whose `owner_id` matches `principal.subject`.
This route SHALL authorize against Output ownership, not Source ownership.
Shared Source rows are expected; they SHALL NOT cause one principal to see another principal's output records.

**Schema reference:** `GET /v1/bookmarks/{aizk_uuid}/outputs` · query param: latest (bool, default false) · response: list of `OutputResponse`

#### Scenario: Retrieve all outputs

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs for the bookmark's internal identifier
- **THEN** all conversion output records are returned ordered by creation time descending

#### Scenario: Retrieve latest output only

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs with the latest flag set
- **THEN** only the most recently created conversion output record is returned

#### Scenario: Shared source returns only caller-owned outputs

- **GIVEN** two principals have Jobs against the same `aizk_uuid`, and successful conversion outputs exist for both owners
- **WHEN** either principal calls `GET /v1/bookmarks/{aizk_uuid}/outputs`
- **THEN** the response contains only outputs whose `owner_id` matches that caller; outputs owned by the other principal are absent from the list

#### Scenario: Cross-owner bookmark query with no owned outputs returns an empty list

- **GIVEN** outputs exist for the requested `aizk_uuid`, but all of them are owned by a different principal
- **WHEN** a client calls `GET /v1/bookmarks/{aizk_uuid}/outputs`
- **THEN** the system returns HTTP 200 with an empty list

### Requirement: Serve raw manifest JSON for a conversion output

The system SHALL expose an endpoint that retrieves and returns the raw manifest JSON for a conversion output record directly from object storage without re-parsing or transforming the content.
The system SHALL return 404 when the resolved `principal.subject` does not match the target output row's `owner_id`.
The response body and status code SHALL be identical to the not-found case so that cross-owner access does not leak output existence.

**Schema reference:** `GET /v1/outputs/{output_id}/manifest` · response: `application/json` raw bytes

#### Scenario: Retrieve manifest for a known output

- **GIVEN** a conversion output record exists with a valid manifest key
- **WHEN** a client requests the manifest by output identifier
- **THEN** the system returns the raw manifest bytes with Content-Type `application/json`

#### Scenario: Manifest object missing from storage

- **GIVEN** a conversion output record exists but its manifest object is absent from storage
- **WHEN** a client requests the manifest
- **THEN** the system returns a 404 response

#### Scenario: Cross-owner manifest read returns 404

- **GIVEN** a conversion output row exists with `owner_id != principal.subject`
- **WHEN** a client requests `GET /v1/outputs/{output_id}/manifest`
- **THEN** the system returns HTTP 404, indistinguishable from a request for a non-existent output id

### Requirement: Serve markdown content for a conversion output

The system SHALL expose an endpoint that retrieves and returns the converted markdown text for a conversion output record directly from object storage.
The system SHALL return 404 when the resolved `principal.subject` does not match the target output row's `owner_id`.
The response body and status code SHALL be identical to the not-found case so that cross-owner access does not leak output existence.

**Schema reference:** `GET /v1/outputs/{output_id}/markdown` · response: `text/markdown; charset=utf-8` raw bytes

#### Scenario: Retrieve markdown for a known output

- **GIVEN** a conversion output record exists with a valid markdown key
- **WHEN** a client requests the markdown by output identifier
- **THEN** the system returns the markdown bytes with Content-Type `text/markdown; charset=utf-8`

#### Scenario: Cross-owner markdown read returns 404

- **GIVEN** a conversion output row exists with `owner_id != principal.subject`
- **WHEN** a client requests `GET /v1/outputs/{output_id}/markdown`
- **THEN** the system returns HTTP 404, indistinguishable from a request for a non-existent output id

### Requirement: Serve figure images for a conversion output

The system SHALL expose an endpoint that retrieves and returns individual figure images for a conversion output record by filename, and SHALL reject filenames that could escape the figures storage prefix.
The system SHALL return 404 when the resolved `principal.subject` does not match the target output row's `owner_id`, using the same not-found posture as the manifest and markdown routes.
The existing filename-validation requirement remains in force.

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

#### Scenario: Cross-owner figure read returns 404

- **GIVEN** a conversion output row exists with `owner_id != principal.subject`
- **WHEN** a client requests `GET /v1/outputs/{output_id}/figures/{filename}`
- **THEN** the system returns HTTP 404, indistinguishable from a request for a non-existent output id

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

### Requirement: Validate auth mode at API startup

The API process SHALL validate the `AIZK_AUTH_MODE` configuration value during startup and SHALL refuse to start if the value is unrecognized or names a mode that is not implemented in the current build.
At cutover, the only implemented mode is `trust_network`; any other value reserved by the type (`token`, `proxy_headers`, `oidc`) or any string outside the recognized set SHALL cause the process to fail to start with a typed startup error and a non-zero exit code.

When `AIZK_AUTH_MODE` is unset, the setting SHALL default to `trust_network` so that a fresh-clone deployment runs without explicit configuration; this matches the shipped defaults for `AIZK_DEFAULT_PRINCIPAL` (`"self"`) and `AIZK_TRUSTED_HOSTS` (`["localhost", "127.0.0.1"]`).
This default does not weaken the trust posture: every recognized mode is rejected unless its resolver branch is implemented, so the only way the process boots is in a mode whose behavior is fully specified.

This requirement guarantees that a misconfigured deployment fails loudly at process boot rather than silently default-opening to an unintended trust posture.

#### Scenario: trust_network mode boots successfully

- **GIVEN** `AIZK_AUTH_MODE=trust_network` and `AIZK_DEFAULT_PRINCIPAL` is set
- **WHEN** the API process starts
- **THEN** the process completes startup, the `/health/live` endpoint returns 200, and request handling proceeds

#### Scenario: Unset auth mode defaults to trust_network

- **GIVEN** `AIZK_AUTH_MODE` is not set in the process environment
- **WHEN** the API process is launched
- **THEN** the setting resolves to `trust_network`, the process completes startup, and request handling proceeds

#### Scenario: Unimplemented auth mode rejected at startup

- **GIVEN** `AIZK_AUTH_MODE=token` (a value reserved for a future change but not implemented at this cutover)
- **WHEN** the API process is launched
- **THEN** startup raises a typed configuration error identifying the mode as not-yet-implemented and the process exits non-zero before binding the HTTP listener

#### Scenario: Unknown auth mode rejected at startup

- **GIVEN** `AIZK_AUTH_MODE=lol_anyone_can_in`
- **WHEN** the API process is launched
- **THEN** startup raises a typed configuration error identifying the value as unrecognized and the process exits non-zero

### Requirement: Resolve a Principal on every API request

The API SHALL resolve a `Principal` value on every inbound request before route handlers execute, and SHALL make the resolved Principal available to handlers via dependency injection.
A `Principal` carries at minimum a `subject: str` identifier and a `provenance` discriminator that names the auth mode that produced it.
At cutover, the only legal `provenance` value is `"trust_network"`; the type SHALL be defined as a discriminated union so that future auth modes (`token`, `proxy_headers`, `oidc`) extend it without changing the routes that consume it.

In `trust_network` mode, the resolver SHALL return `Principal(subject=AIZK_DEFAULT_PRINCIPAL, provenance="trust_network")` for every request without inspecting the request body or auth-bearing headers.
The Principal value is the same for every request in this mode by design — `trust_network` is single-principal.

This requirement does not by itself create or persist anything; it establishes the contract that every request handler can see "who" the request belongs to without each handler inventing its own resolution path.

#### Scenario: Principal resolved on every request in trust_network mode

- **GIVEN** `AIZK_AUTH_MODE=trust_network` and `AIZK_DEFAULT_PRINCIPAL=local`
- **WHEN** any API request reaches a route handler
- **THEN** the handler receives a `Principal(subject="self", provenance="trust_network")` via the dependency, regardless of route or method

#### Scenario: Principal resolution does not consult auth headers in trust_network mode

- **GIVEN** the request carries `Authorization: Bearer <anything>` or `X-Forwarded-User: someone-else`
- **WHEN** the Principal dependency runs
- **THEN** the resolved `subject` equals `AIZK_DEFAULT_PRINCIPAL` and `provenance == "trust_network"`; the request headers do not influence the resolution

### Requirement: Enforce trusted-host allowlist on every request

Every inbound request SHALL be checked against the `AIZK_TRUSTED_HOSTS` allowlist before route handling proceeds.
A request whose `Host` header does not match a value in the allowlist SHALL be rejected with HTTP 400 (the Starlette `TrustedHostMiddleware` default), and the route handler SHALL NOT execute.
The check SHALL operate on the actual `Host` header that reaches the API process.
`Forwarded` and `X-Forwarded-Host` SHALL NOT be honored as input to this check; reverse-proxy deployments are responsible for rewriting `Host` to a trusted value before forwarding (and for stripping any client-supplied `X-Forwarded-Host`).

The shipped default for `AIZK_TRUSTED_HOSTS` is `["localhost", "127.0.0.1"]` so that a fresh clone runs out of the box.
Operators deploying behind a reverse proxy or ingress gateway SHALL override this setting to the public-facing hostname; the configuration documentation SHALL call this out as a deployment requirement.

This requirement is defense-in-depth for the `trust_network` posture: the network is presumed trusted, but a misconfigured ingress that forwards arbitrary `Host` headers should not allow DNS-rebinding-style attacks against the API.

#### Scenario: Request to allowed host accepted

- **GIVEN** `AIZK_TRUSTED_HOSTS=["api.example.internal"]` and a request with `Host: api.example.internal`
- **WHEN** the request reaches the API
- **THEN** the trusted-host check passes and the route handler executes normally

#### Scenario: Request to disallowed host rejected

- **GIVEN** `AIZK_TRUSTED_HOSTS=["api.example.internal"]` and a request with `Host: evil.example.com`
- **WHEN** the request reaches the API
- **THEN** the API returns HTTP 400 and the route handler does not execute

#### Scenario: Default allowlist permits localhost

- **GIVEN** `AIZK_TRUSTED_HOSTS` is not set (default applies) and a request with `Host: localhost`
- **WHEN** the request reaches the API
- **THEN** the trusted-host check passes

### Requirement: Principal abstraction is intentionally extensible (non-normative)

The `Principal` type and `get_principal` dependency are designed so that future auth modes (`token`, `proxy_headers`, `oidc`) plug in without changing route handlers, materialization helpers, or the database schema introduced by this change.
This requirement records the design intent so that a future spec change adding one of those modes is a delta on the `Principal` type and the resolver, not a refactor of every route.

This requirement is non-normative — it has no scenarios.
The normative requirements above (`Validate auth mode at API startup`, `Resolve a Principal on every API request`) define the observable behavior; this requirement is a marker that the future widening is anticipated.

## Technical Notes

- **Implementation**: `src/aizk/conversion/api/`
- **Routes**:
  - `POST /v1/jobs` — submit job
  - `GET /v1/jobs` — list jobs (filters: status, source_id, created_after, created_before; pagination: limit, offset)
  - `GET /v1/jobs/status-counts` — aggregate counts by status
  - `GET /v1/jobs/{job_id}` — get single job
  - `POST /v1/jobs/{job_id}/retry` — retry failed/cancelled job
  - `POST /v1/jobs/{job_id}/cancel` — cancel queued/running job
  - `POST /v1/jobs/actions` — bulk retry or cancel (1–100 job IDs)
  - `GET /v1/bookmarks/{source_id}/outputs` — list conversion outputs for a bookmark
  - `GET /v1/outputs/{output_id}/manifest` — raw manifest JSON from S3
  - `GET /v1/outputs/{output_id}/markdown` — markdown text from S3
  - `GET /v1/outputs/{output_id}/figures/{filename}` — figure image from S3
  - `GET /health/live` — liveness probe (no dependency checks)
  - `GET /health/ready` — readiness probe (DB + S3 + optional picture description health checks)
- **Dependencies**: conversion-worker (data model); conversion-ui (served under same process)
- **Idempotency key**: computed by the worker as a hash of bookmark identifier, payload version, Docling version, and config hash; the API surface accepts a client-supplied key as an override
- **Readiness probe shape**: database check via a short-lived connection; S3 check via HEAD bucket; picture description check issues `GET {base_url}/models` with an `Authorization: Bearer` header and a 5-second per-check timeout
- **Indexes**: composite index on `(status, earliest_next_attempt_at, queued_at)` on the conversion jobs table supports job claiming and queue-depth counting without full table scans
