# Delta for Conversion API — Job Visibility Permissions

## MODIFIED Requirements

### Requirement: List jobs with filters and pagination

The system SHALL filter the job list to jobs whose `owner_id` matches `principal.subject`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected into the handler on every request. (Previously: all jobs returned regardless of `owner_id`; `get_principal` not injected.)

#### Scenario: List returns only caller-owned jobs

- **GIVEN** jobs exist with two distinct `owner_id` values and `AIZK_AUTH_MODE=trust_network`
- **WHEN** a client calls `GET /v1/jobs`
- **THEN** only jobs whose `owner_id` matches `principal.subject` are returned; jobs owned
  by other principals are absent from the result and the `total` count

#### Scenario: trust_network list is unchanged

- **GIVEN** `AIZK_AUTH_MODE=trust_network` and all jobs share `owner_id = AIZK_DEFAULT_PRINCIPAL`
- **WHEN** a client calls `GET /v1/jobs`
- **THEN** the response is identical to the pre-change behavior (all jobs visible, filter is
  a no-op because every job is owned by the single principal)

---

### Requirement: Retrieve individual job status

The system SHALL return 404 when the resolved `principal.subject` does not match the job's `owner_id`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected into the handler on every request.
The response body and status code SHALL be identical to the not-found case so that cross-owner access does not leak job existence. (Previously: any job retrievable by id regardless of `owner_id`; `get_principal` not injected.)

#### Scenario: Owner retrieves their own job

- **GIVEN** a job exists with `owner_id = principal.subject`
- **WHEN** a client calls `GET /v1/jobs/{job_id}`
- **THEN** the job details are returned with HTTP 200

#### Scenario: Cross-owner get returns 404

- **GIVEN** a job exists with `owner_id != principal.subject`
- **WHEN** a client calls `GET /v1/jobs/{job_id}` for that job
- **THEN** the system returns HTTP 404 with `error: job_not_found`; the response is
  indistinguishable from a request for a non-existent job id

---

### Requirement: Return aggregate job status counts

The system SHALL count only jobs whose `owner_id` matches `principal.subject`.
The `principal` SHALL be resolved via the `get_principal` dependency and injected into the handler on every request. (Previously: counts are global across all jobs; `get_principal` not injected.)

#### Scenario: Status counts are owner-scoped

- **GIVEN** jobs exist for two distinct owners and `AIZK_AUTH_MODE` admits multiple principals
- **WHEN** each principal calls `GET /v1/jobs/status-counts`
- **THEN** each response reflects only that principal's jobs; the two responses need not sum
  to the global total

#### Scenario: trust_network counts are unchanged

- **GIVEN** `AIZK_AUTH_MODE=trust_network`
- **WHEN** a client calls `GET /v1/jobs/status-counts`
- **THEN** the response is identical to the pre-change behavior (all jobs counted, filter is
  a no-op)

---

### Requirement: Retry failed jobs

The system SHALL return 404 when the resolved `principal.subject` does not match the
target job's `owner_id`, using the same response body as the not-found case.
(Previously: `principal` injected but not used for authorization enforcement.)

#### Scenario: Owner retries their own job

- **GIVEN** a job with `owner_id = principal.subject` and status `FAILED_RETRYABLE`
- **WHEN** a client posts `POST /v1/jobs/{job_id}/retry`
- **THEN** the job is reset to `QUEUED` and the updated job is returned

#### Scenario: Cross-owner retry returns 404

- **GIVEN** a job with `owner_id != principal.subject`
- **WHEN** a client posts `POST /v1/jobs/{job_id}/retry`
- **THEN** the system returns HTTP 404 with `error: job_not_found`

---

### Requirement: Cancel jobs

The system SHALL return 404 when the resolved `principal.subject` does not match the
target job's `owner_id`, using the same response body as the not-found case.
(Previously: `principal` injected but not used for authorization enforcement.)

#### Scenario: Owner cancels their own job

- **GIVEN** a job with `owner_id = principal.subject` and status `QUEUED`
- **WHEN** a client posts `POST /v1/jobs/{job_id}/cancel`
- **THEN** the job transitions to `CANCELLED` and the updated job is returned

#### Scenario: Cross-owner cancel returns 404

- **GIVEN** a job with `owner_id != principal.subject`
- **WHEN** a client posts `POST /v1/jobs/{job_id}/cancel`
- **THEN** the system returns HTTP 404 with `error: job_not_found`

---

### Requirement: Apply bulk actions across multiple jobs

The system SHALL treat each job in the request independently: a job whose `owner_id` does not match `principal.subject` SHALL be returned with `status: "error"` and `error: "job_not_found"` in the per-job result, consistent with the existing not-found error path and the 404-posture for cross-owner access.
The presence of cross-owner job ids in the request SHALL NOT cause the entire bulk operation to fail; eligible owned jobs SHALL still be actioned. (Previously: `principal` injected but not used for authorization enforcement.)

#### Scenario: Bulk action skips cross-owner jobs

- **GIVEN** a bulk request containing one job owned by the caller and one job owned by a
  different principal
- **WHEN** a client posts `POST /v1/jobs/actions`
- **THEN** the owned job is actioned and returned with `status: "success"`; the cross-owner
  job is returned with `status: "error"` and `error: "job_not_found"`; the summary
  `errors` count includes the cross-owner job
