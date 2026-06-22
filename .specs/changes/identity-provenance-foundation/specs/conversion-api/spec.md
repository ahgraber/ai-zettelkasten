# Delta for conversion-api

## MODIFIED Requirements

### Requirement: Accept job submission without external service calls

The system SHALL accept conversion job submissions via a REST endpoint whose request body types `source_ref` as the narrow `IngressSourceRef` discriminated union (required), and SHALL enqueue the job without invoking any external services during request handling.
At cutover `IngressSourceRef` admits only `KarakeepBookmarkRef`; widening the admitted set is a deployment-config change via `IngressPolicy` and does not alter the internal `SourceRef` contract.
The `karakeep_id` field is removed from the request body; callers SHALL submit `KarakeepBookmarkRef` as the `source_ref` variant instead. (Previously: the endpoint accepted a `karakeep_id` string field.
Now it accepts only `source_ref`.)

Serves: coherent-pipeline-foundation

> Previously: the durable source identity was named `aizk_uuid`.

The API SHALL materialize the Source row at submit time: parse `source_ref` against `IngressSourceRef`, gate its `kind` against `SubmissionCapabilities.accepted_submission_kinds`, canonicalize via the variant's `to_dedup_payload()`, compute the matching `source_ref_hash`, create or reuse a Source row keyed on that hash, and persist the job with the resulting `source_id` FK — the Source's durable identity, distinct from the `source_ref_hash` matching key.
The stored `Source.source_ref` and the denormalized `Job.source_ref` retain the wide `SourceRef` type so that future widening of `IngressPolicy` does not require a schema change.
Source reuse under concurrent submission SHALL use `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` followed by `SELECT` on the hash so that two simultaneous submissions of the same `source_ref` share a single Source row; distinct jobs MAY still be created and are deduplicated at the job level by `(owner_id, idempotency_key)`.
Job-level deduplication SHALL be owner-scoped: duplicate detection SHALL match an existing job only when both `idempotency_key` and `owner_id` match the resolved principal.
The API SHALL treat `(principal.subject, idempotency_key)` as the duplicate-submission identity, while preserving the existing key material (`source_ref_hash`, converter name, output-affecting config snapshot).
Two principals submitting the same `source_ref_hash` MAY still share a single Source row; that shared Source row SHALL NOT cause the second principal to reuse the first principal's Job row.
For `KarakeepBookmarkRef` submissions, the API SHALL populate `Source.karakeep_id` from `bookmark_id`; for all other variants (once admitted by `IngressPolicy`), `karakeep_id` SHALL be null.
The API SHALL compute the idempotency key at submit time, including `source_ref_hash`, `converter_name`, and the converter's output-affecting config snapshot in the hash, so that jobs for different source refs, different converters, or different converter configurations produce distinct keys.
This idempotency formula replaces the pre-refactor formula (which hashed `source_id` and Docling-specific fields).
Replay-idempotency across the cutover is preserved by the `bookmarks → sources` migration using a canonical-job strategy: for each source, the migration designates the most-recent SUCCEEDED job (falling back to the most-recent job by id) as the canonical historical job and rewrites its `idempotency_key` to `sha256("{source_ref_hash}:docling:{frozen_config_json}")`, which matches `compute_idempotency_key()`.
Accordingly, a post-migration re-submission of the same source with default config SHALL hit the canonical historical job and not produce a new job.
Additional historical jobs for the same source (i.e., when multiple jobs existed pre-migration) receive a job-id-suffixed key that is stable and unique but not reachable by re-submission.
Historical jobs originally submitted with non-default config cannot be restored to continuity — the original config is not recoverable at migration time — so re-submissions with non-default config will still create new jobs regardless.
Source identity columns (`source_id`, `source_ref`, `source_ref_hash`, `karakeep_id`) SHALL be immutable after creation; worker writes are confined to mutable metadata columns.

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
- **THEN** exactly one Source row exists for that hash, both jobs reference its `source_id`, and job-level deduplication proceeds via `idempotency_key`

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

### Requirement: List jobs with filters and pagination

The system SHALL expose an endpoint to list conversion jobs filterable by status, internal source identifier, and supporting pagination.
The system SHALL filter the job list to jobs whose `owner_id` matches `principal.subject`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected into the handler on every request. (Previously: all jobs returned regardless of `owner_id`; `get_principal` not injected.)

Serves: coherent-pipeline-foundation

> Previously: the durable source identity was named `aizk_uuid`.

**Schema reference:** `GET /v1/jobs` · query params: status, source_id, created_after, created_before, limit (1–1000, default 50), offset (≥0, default 0) · response: `JobList`

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

### Requirement: Retrieve conversion outputs for a bookmark

The system SHALL expose an endpoint returning all conversion output records for a bookmark ordered by creation time descending, with an option to return only the most recent output.
The system SHALL scope bookmark-output listing to `ConversionOutput.owner_id`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected into the handler on every request.
The response SHALL include only output rows whose `source_id` matches the requested bookmark identifier **and** whose `owner_id` matches `principal.subject`.
This route SHALL authorize against Output ownership, not Source ownership.
Shared Source rows are expected; they SHALL NOT cause one principal to see another principal's output records.

Serves: coherent-pipeline-foundation

> Previously: the durable source identity was named `aizk_uuid`.

**Schema reference:** `GET /v1/bookmarks/{source_id}/outputs` · query param: latest (bool, default false) · response: list of `OutputResponse`

#### Scenario: Retrieve all outputs

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs for the bookmark's internal identifier
- **THEN** all conversion output records are returned ordered by creation time descending

#### Scenario: Retrieve latest output only

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs with the latest flag set
- **THEN** only the most recently created conversion output record is returned

#### Scenario: Shared source returns only caller-owned outputs

- **GIVEN** two principals have Jobs against the same `source_id`, and successful conversion outputs exist for both owners
- **WHEN** either principal calls `GET /v1/bookmarks/{source_id}/outputs`
- **THEN** the response contains only outputs whose `owner_id` matches that caller; outputs owned by the other principal are absent from the list

#### Scenario: Cross-owner bookmark query with no owned outputs returns an empty list

- **GIVEN** outputs exist for the requested `source_id`, but all of them are owned by a different principal
- **WHEN** a client calls `GET /v1/bookmarks/{source_id}/outputs`
- **THEN** the system returns HTTP 200 with an empty list
