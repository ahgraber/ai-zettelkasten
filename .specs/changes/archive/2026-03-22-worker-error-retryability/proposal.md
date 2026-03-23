# Proposal: worker-error-retryability

## Intent

A spec-vs-code review of the `worker-process-management` baseline spec against the current implementation reveals three exception classes that still lack an explicit `retryable: ClassVar[bool]` attribute, and two sites in `worker.py` that still use `getattr(error, "retryable", True)` as a fallback.
These violate the requirement that error retryability SHALL be determined via an explicit class attribute without `getattr` fallbacks.

The previous change (`2026-03-22-worker-spec-gaps`) corrected `S3Error` and `S3UploadError`.
This change closes the remaining gaps in the exception hierarchy and removes the now-unnecessary `getattr` fallbacks.

## Scope

**In scope:**

- Add `retryable: ClassVar[bool] = False` to `ConversionArtifactsMissingError` (missing artifacts
  are a permanent failure — the content will not appear on retry)
- Add `retryable: ClassVar[bool] = False` to `ConversionCancelledError` (a user-cancelled job is
  not retryable)
- Add `retryable: ClassVar[bool] = True` to `ReportedChildError` as the class-level default,
  preserving the existing optional per-instance override
- Remove the `getattr(error, "retryable", True)` fallback from `handle_job_error()` and from
  `_process_job_subprocess()` once all exception classes carry the attribute

**Out of scope:**

- Changing the retry behaviour or backoff logic
- Adding new error classes or modifying error codes
- Any other spec or implementation changes

## Approach

1. Add the missing `retryable: ClassVar[bool]` attributes to `ConversionArtifactsMissingError`,
   `ConversionCancelledError`, and `ReportedChildError` in `worker.py`.
2. Once all exception classes carry the attribute, replace the two `getattr(error, "retryable", True)`
   calls with direct attribute access (`error.retryable`).
3. Update unit tests to assert retryability on the previously-uncovered classes.
