# Delta for conversion-api

## MODIFIED Requirements

### Requirement: Accept job submission without external service calls

The system SHALL accept conversion job submissions via a REST endpoint receiving a `source_ref` discriminated union (required), and SHALL enqueue the job without invoking any external services during request handling.
The `karakeep_id` field is removed from the request body; callers SHALL submit `KarakeepBookmarkRef` as the `source_ref` variant instead. (Previously: the endpoint accepted a `karakeep_id` string field.
Now it accepts only `source_ref`.)

The API SHALL materialize Source identity at submit time: validate `source_ref`, canonicalize via the variant's `to_dedup_payload()`, compute `source_ref_hash`, create or reuse a Source row keyed on the hash, and persist the job with the resulting `aizk_uuid` FK.
Source reuse under concurrent submission SHALL use `INSERT ... ON CONFLICT (source_ref_hash) DO NOTHING` followed by `SELECT` on the hash so that two simultaneous submissions of the same `source_ref` share a single Source row; distinct jobs MAY still be created and are deduplicated at the job level by `idempotency_key`.
For `KarakeepBookmarkRef` submissions, the API SHALL populate `Source.karakeep_id` from `bookmark_id`; for all other variants, `karakeep_id` SHALL be null.
The API SHALL compute the idempotency key at submit time, including `source_ref_hash`, `converter_name`, and the converter's output-affecting config snapshot in the hash, so that jobs for different source refs, different converters, or different converter configurations produce distinct keys.
This idempotency formula intentionally differs from the pre-refactor formula (which hashed `aizk_uuid` and Docling-specific fields).
Post-cutover idempotency-key compatibility with pre-refactor jobs is NOT guaranteed; the structural contract is that the converter adapter's `config_snapshot()` contributes the same output-affecting field set as before — not that resulting hashes collide.
Source identity columns (`aizk_uuid`, `source_ref`, `source_ref_hash`, `karakeep_id`) SHALL be immutable after creation; worker writes are confined to mutable metadata columns.

**Schema reference:** `POST /v1/jobs` · request: `JobSubmission` (updated) · response: `JobResponse` (updated)

#### Scenario: Submit with source_ref

- **GIVEN** a valid `SourceRef` (e.g., `KarakeepBookmarkRef`, `UrlRef`)
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

#### Scenario: Unregistered source_ref kind rejected

- **GIVEN** the deployment has fetcher adapters registered for kinds `{"karakeep_bookmark"}` only
- **WHEN** a client submits a job with `source_ref.kind = "url"`
- **THEN** HTTP 422 is returned with an error indicating the kind is not supported in this deployment

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

## ADDED Requirements

### Requirement: Gate accepted source ref kinds via DeploymentCapabilities

The API SHALL validate `source_ref.kind` against a `DeploymentCapabilities` descriptor supplied by the wiring layer, and SHALL reject submissions whose kind is not in `accepted_kinds` with HTTP 422.
`accepted_kinds` is the set of kinds the composition root registered; adapters that are not yet ready are not registered (their skeleton classes may exist in code but are not wired).
See the `pluggable-pipeline` delta for the descriptor's contract.

#### Scenario: Registered kind accepted

- **GIVEN** `DeploymentCapabilities.accepted_kinds = {"karakeep_bookmark"}` because the composition root registered that kind
- **WHEN** a client submits a job with `source_ref.kind = "karakeep_bookmark"`
- **THEN** the submission is accepted

#### Scenario: Unregistered kind rejected

- **GIVEN** `DeploymentCapabilities.accepted_kinds = {"karakeep_bookmark"}`
- **WHEN** a client submits a job with `source_ref.kind = "url"`
- **THEN** HTTP 422 is returned with an error indicating the kind is not supported in this deployment
