# Delta for conversion-api

## MODIFIED Requirements

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
Replay-idempotency is guaranteed for post-migration submissions only.
The `bookmarks → sources` migration rewrites historical `idempotency_key` values using a frozen default config snapshot (see the `schema-migrations` delta); historical rows for which the original per-job config is unrecoverable receive a disambiguated key that preserves uniqueness but does not guarantee deduplication on re-submission.
A re-submission after migration will always produce a fresh key computed from the live deployment config, and it will not match a migrated historical key because migrated rows are intentionally disambiguated.
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

_Note: this scenario is not reachable at cutover because `IngressSourceRef` is still a narrow union that only admits `KarakeepBookmarkRef`; pydantic rejects any other kind at the schema layer before the policy gate runs._
_This scenario becomes exercisable once `IngressSourceRef` is widened to include a second kind that is registered for worker dispatch but excluded from `IngressPolicy`._

- **GIVEN** `DeploymentCapabilities.registered_kinds` includes `"url"` (worker can dispatch it) but `IngressPolicy` does not admit `"url"`, so `SubmissionCapabilities.accepted_submission_kinds` excludes it, AND `IngressSourceRef` has been widened to include `UrlRef`
- **WHEN** a client submits a job with `source_ref.kind = "url"`
- **THEN** HTTP 422 is returned with an error indicating the kind is not admitted for submission in this deployment
