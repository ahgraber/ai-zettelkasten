# Delta for Worker Process Management

> Generated from code analysis on 2026-03-22
> Source files: src/aizk/conversion/workers/worker.py, src/aizk/conversion/storage/s3_client.py

## MODIFIED Requirements

### Requirement: Classify errors as retryable or permanent

The system SHALL classify each error type as retryable or permanent **via an explicit
`retryable: bool` class attribute on every exception class**, and SHALL use this classification
to determine the resulting job status without relying on error message matching or `getattr`
fallbacks. *(Previously: `S3Error` and `S3UploadError` lacked the explicit attribute, relying on
the `getattr(..., True)` default in `handle_job_error()`; all other exception classes carried the
attribute explicitly.)*

#### Scenario: S3 upload error classified as retryable

- **GIVEN** an S3 upload fails due to a transient error
- **WHEN** `handle_job_error()` inspects the exception
- **THEN** the `retryable` attribute is read directly from the exception class (not a default),
  and the job transitions to FAILED_RETRYABLE

### Requirement: Skip processing of cancelled queued jobs

The system SHALL not begin processing a job that is already CANCELLED when a worker picks it up.
The CANCELLED status check SHALL occur before the job is transitioned to RUNNING in the database.
_(Previously: the check happened after the RUNNING transition had already been committed.)_

#### Scenario: Cancelled queued job skipped by worker

- **GIVEN** a job is QUEUED and then cancelled before any worker starts it
- **WHEN** a worker polls for work and selects the job
- **THEN** the worker detects CANCELLED status before setting RUNNING and exits immediately
  without starting the subprocess

#### Scenario: Job cancelled between poll and running transition

- **GIVEN** a job is selected by the worker but cancelled before the RUNNING state is set
- **WHEN** the worker enters the supervised processing function
- **THEN** it detects the CANCELLED status **before issuing the RUNNING update** and exits
  without starting the conversion subprocess
