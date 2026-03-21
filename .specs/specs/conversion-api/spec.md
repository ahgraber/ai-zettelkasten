# Conversion API Specification

> Translated from Spec Kit on 2026-03-21
> Source: specs/001-docling-conversion-service/spec.md

## Purpose

The Conversion API exposes REST endpoints for submitting, querying, retrying, and cancelling bookmark conversion jobs.
It accepts requests from client applications and enqueues work for the conversion worker without invoking external services during request handling.
It also surfaces conversion outputs and aggregate job status metrics.

## Requirements

### Requirement: Accept job submission without external service calls

The system SHALL accept bookmark conversion job submissions via a REST endpoint receiving a KaraKeep bookmark identifier and optional payload version and idempotency key, and SHALL enqueue the job without invoking any external services during request handling.

#### Scenario: Submit single bookmark for conversion

- **GIVEN** a valid KaraKeep bookmark identifier
- **WHEN** a client submits a conversion job via the API
- **THEN** the system creates a job record, returns the job identifier and initial status, and the bookmark URL, title, and content type may be null until the worker processes the job

#### Scenario: Submission does not call external services

- **GIVEN** a job submission is received
- **WHEN** the API handler processes the request
- **THEN** no calls are made to KaraKeep or any external service during request handling

### Requirement: Reject duplicate job submissions

The system SHALL reject job submissions whose computed idempotency key matches an existing job record.

#### Scenario: Duplicate idempotency key rejected

- **GIVEN** a bookmark with an identical idempotency key already exists
- **WHEN** a client resubmits the same bookmark
- **THEN** the system returns the existing job details without creating a new record

### Requirement: Retrieve individual job status

The system SHALL expose an endpoint to retrieve the status, timestamps, attempt count, error details, and artifact summary for a single job.

#### Scenario: Get status of succeeded job

- **GIVEN** a job has completed successfully
- **WHEN** a client requests the job by identifier
- **THEN** the response includes status, timestamps, attempt count, and a summary of conversion artifacts

#### Scenario: Get status of failed job

- **GIVEN** a job has failed
- **WHEN** a client requests the job by identifier
- **THEN** the response includes status, error code, error message, and attempt count

### Requirement: List jobs with filters and pagination

The system SHALL expose an endpoint to list conversion jobs filterable by status, internal bookmark identifier, KaraKeep identifier, and supporting pagination.

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

#### Scenario: Status counts returned

- **GIVEN** jobs exist in various statuses
- **WHEN** a client requests the status counts endpoint
- **THEN** the response returns a count for each status value present in the system

### Requirement: Retry failed jobs

The system SHALL expose an endpoint to retry a failed or permanently failed job by resetting its status to queued and incrementing its attempt count.

#### Scenario: Retry a failed-retryable job

- **GIVEN** a job has status FAILED_RETRYABLE or FAILED_PERM
- **WHEN** a client posts a retry request for that job
- **THEN** the job status resets to QUEUED, the attempt count increments, and the retry scheduling timestamp is cleared

### Requirement: Cancel jobs

The system SHALL expose an endpoint to cancel queued or running jobs on a best-effort basis.

#### Scenario: Cancel a queued job

- **GIVEN** a job has status QUEUED
- **WHEN** a client posts a cancel request for that job
- **THEN** the job transitions to CANCELLED and will not be processed

#### Scenario: Cancel a running job

- **GIVEN** a job has status RUNNING
- **WHEN** a client posts a cancel request for that job
- **THEN** the system attempts best-effort cancellation and updates the job status to CANCELLED

### Requirement: Submit batch of jobs

The system SHALL expose an endpoint accepting an array of job submissions and processing each independently, returning per-item results.

#### Scenario: Batch with all valid submissions

- **GIVEN** a batch of valid job submissions is sent
- **WHEN** the API processes the batch
- **THEN** each job is created independently and the response includes per-item job identifiers and status

#### Scenario: Batch with mixed valid and invalid submissions

- **GIVEN** a batch contains both valid and duplicate-idempotency-key submissions
- **WHEN** the API processes the batch
- **THEN** valid entries produce new job records and duplicate entries return the existing job details, all reported per-item

### Requirement: Apply bulk actions across multiple jobs

The system SHALL expose an endpoint accepting a list of job identifiers and a bulk action (retry or cancel) to apply to all specified jobs.

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

#### Scenario: Retrieve all outputs

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs for the bookmark's internal identifier
- **THEN** all conversion output records are returned ordered by creation time descending

#### Scenario: Retrieve latest output only

- **GIVEN** a bookmark has multiple successful conversions
- **WHEN** a client requests outputs with the latest flag set
- **THEN** only the most recently created conversion output record is returned

## Technical Notes

- **Implementation**: `aizk/conversion/api/`
- **Dependencies**: conversion-worker (data model); conversion-ui (served under same process)
- **Idempotency key**: computed by the worker as a hash of bookmark identifier, payload version, Docling version, and config hash; the API surface accepts a client-supplied key as an override
