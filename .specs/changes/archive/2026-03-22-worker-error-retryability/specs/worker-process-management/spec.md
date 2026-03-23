# Delta for Worker Process Management

> Generated from code analysis on 2026-03-22
> Source files: src/aizk/conversion/workers/worker.py, src/aizk/conversion/storage/s3_client.py

## MODIFIED Requirements

### Requirement: Classify errors as retryable or permanent

The system SHALL classify each error type as retryable or permanent via an explicit
`retryable: bool` class attribute on every exception class, and SHALL use this classification
to determine the resulting job status without relying on error message matching or `getattr`
fallbacks.

The following exception classes SHALL carry the attribute:

| Class                             | `retryable` value      | Rationale                                                                             |
| --------------------------------- | ---------------------- | ------------------------------------------------------------------------------------- |
| `ConversionArtifactsMissingError` | `False`                | Missing artifacts indicate a permanent data failure; retrying will not produce output |
| `ConversionCancelledError`        | `False`                | Job was explicitly cancelled by the user; retrying is not appropriate                 |
| `ConversionTimeoutError`          | `True`                 | Transient; fresh timeout window on retry                                              |
| `ConversionSubprocessError`       | `True`                 | Transient subprocess crash; eligible for retry                                        |
| `JobDataIntegrityError`           | `False`                | Non-recoverable data invariant violation                                              |
| `PreflightError`                  | `True`                 | Transient preflight failure; eligible for retry                                       |
| `ReportedChildError`              | `True` (class default) | Child errors default to retryable; individual instances may override                  |
| `S3Error`                         | `True`                 | Transient storage error                                                               |
| `S3UploadError`                   | `True`                 | Transient upload error                                                                |

`handle_job_error()` and `_process_job_subprocess()` SHALL read `error.retryable` directly,
without a `getattr` fallback. *(Previously: two call sites used `getattr(error, "retryable", True)`,
and `ConversionArtifactsMissingError`, `ConversionCancelledError`, and `ReportedChildError` lacked
a class-level `retryable` attribute.)*

#### Scenario: Permanent error for missing artifacts

- **GIVEN** conversion output artifacts are missing after the subprocess completes
- **WHEN** `handle_job_error()` processes the `ConversionArtifactsMissingError`
- **THEN** the `retryable` attribute is read directly from the exception class (value: `False`),
  and the job transitions to `FAILED_PERM`

#### Scenario: Cancelled error classified without getattr

- **GIVEN** a job is cancelled during processing and `ConversionCancelledError` is raised
- **WHEN** `handle_job_error()` inspects the exception (after the CANCELLED early-return guard)
- **THEN** the `retryable` attribute is read directly from the exception class (value: `False`)

#### Scenario: Child-reported error with no explicit retryability uses class default

- **GIVEN** the conversion subprocess reports a failure without specifying retryability
- **WHEN** `handle_job_error()` processes the resulting `ReportedChildError`
- **THEN** the class-level `retryable = True` default applies, classifying the job as
  `FAILED_RETRYABLE`
