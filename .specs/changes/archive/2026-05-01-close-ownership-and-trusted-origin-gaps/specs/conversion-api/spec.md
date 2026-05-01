# Delta for Conversion API — close-ownership-and-trusted-origin-gaps

## MODIFIED Requirements

### Requirement: Accept job submission without external service calls

Job-level deduplication SHALL be owner-scoped.
When the API computes an idempotency key at submit time, duplicate detection SHALL
match an existing job only when both `idempotency_key` and `owner_id` match the
resolved principal.
The API SHALL therefore treat `(principal.subject, idempotency_key)` as the
duplicate-submission identity, while preserving the existing key material
(`source_ref_hash`, converter name, output-affecting config snapshot).

Source reuse semantics are unchanged: two principals submitting the same
`source_ref_hash` MAY still share a single Source row.
That shared Source row SHALL NOT cause the second principal to reuse the first
principal's Job row.

(Previously: the spec described job-level deduplication only by `idempotency_key`,
which allowed a cross-owner match to satisfy duplicate submission.)

#### Scenario: Different owners share a Source but not a Job

- **GIVEN** principal A has already submitted a source/config pair, producing a Source
  row with hash `H` and a Job whose `idempotency_key = K`
- **AND** principal B submits the same source/config pair, so the computed
  `source_ref_hash = H` and `idempotency_key = K`
- **WHEN** the API handles principal B's submission
- **THEN** the existing Source row MAY be reused, but duplicate detection does not
  match principal A's Job, and a new Job owned by principal B is created

---

### Requirement: Reject duplicate job submissions

The system SHALL return an existing job only when the computed idempotency key
matches a job whose `owner_id` also matches `principal.subject`.
A job with the same `idempotency_key` but a different `owner_id` SHALL NOT satisfy
the duplicate-submission path.

#### Scenario: Same-owner duplicate returns the existing job

- **GIVEN** principal A has an existing job whose `idempotency_key = K`
- **WHEN** principal A resubmits the same source/config and the API computes
  `idempotency_key = K`
- **THEN** the system returns the existing job details without creating a new record

#### Scenario: Cross-owner duplicate key does not return another owner's job

- **GIVEN** principal A has an existing job whose `idempotency_key = K`
- **WHEN** principal B submits a source/config pair that computes the same
  `idempotency_key = K`
- **THEN** the system does not return principal A's job and proceeds as a new
  submission for principal B

---

### Requirement: Retrieve conversion outputs for a bookmark

The system SHALL scope bookmark-output listing to `ConversionOutput.owner_id`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected
into the handler on every request.
The response SHALL include only output rows whose `aizk_uuid` matches the requested
bookmark identifier **and** whose `owner_id` matches `principal.subject`.

This route SHALL authorize against Output ownership, not Source ownership.
Shared Source rows are expected; they SHALL NOT cause one principal to see another
principal's output records.

(Previously: output listing was specified only by bookmark identifier, with no
owner-scope clause.)

#### Scenario: Shared source returns only caller-owned outputs

- **GIVEN** two principals have Jobs against the same `aizk_uuid`, and successful
  conversion outputs exist for both owners
- **WHEN** either principal calls `GET /v1/bookmarks/{aizk_uuid}/outputs`
- **THEN** the response contains only outputs whose `owner_id` matches that caller;
  outputs owned by the other principal are absent from the list

#### Scenario: Cross-owner bookmark query with no owned outputs returns an empty list

- **GIVEN** outputs exist for the requested `aizk_uuid`, but all of them are owned by
  a different principal
- **WHEN** a client calls `GET /v1/bookmarks/{aizk_uuid}/outputs`
- **THEN** the system returns HTTP 200 with an empty list

---

### Requirement: Serve raw manifest JSON for a conversion output

The system SHALL return 404 when the resolved `principal.subject` does not match the
target output row's `owner_id`.
The response body and status code SHALL be identical to the not-found case so that
cross-owner access does not leak output existence.

(Previously: the route was specified only by `output_id` existence.)

#### Scenario: Cross-owner manifest read returns 404

- **GIVEN** a conversion output row exists with `owner_id != principal.subject`
- **WHEN** a client requests `GET /v1/outputs/{output_id}/manifest`
- **THEN** the system returns HTTP 404, indistinguishable from a request for a
  non-existent output id

---

### Requirement: Serve markdown content for a conversion output

The system SHALL return 404 when the resolved `principal.subject` does not match the
target output row's `owner_id`.
The response body and status code SHALL be identical to the not-found case so that
cross-owner access does not leak output existence.

(Previously: the route was specified only by `output_id` existence.)

#### Scenario: Cross-owner markdown read returns 404

- **GIVEN** a conversion output row exists with `owner_id != principal.subject`
- **WHEN** a client requests `GET /v1/outputs/{output_id}/markdown`
- **THEN** the system returns HTTP 404, indistinguishable from a request for a
  non-existent output id

---

### Requirement: Serve figure images for a conversion output

The system SHALL return 404 when the resolved `principal.subject` does not match the
target output row's `owner_id`, using the same not-found posture as the manifest and
markdown routes.
The existing filename-validation requirement remains in force.

(Previously: the route was specified only by `output_id` existence plus filename
validation.)

#### Scenario: Cross-owner figure read returns 404

- **GIVEN** a conversion output row exists with `owner_id != principal.subject`
- **WHEN** a client requests `GET /v1/outputs/{output_id}/figures/{filename}`
- **THEN** the system returns HTTP 404, indistinguishable from a request for a
  non-existent output id
