# Proposal: worker-spec-gaps

## Intent

A spec-vs-code review of `conversion-worker` and `worker-process-management` uncovered three gaps
between the baseline specs and the current implementation:

1. **S3 hash dedup is unimplemented** — the spec requires reusing existing S3 artifacts when the
   content hash is unchanged, but the worker unconditionally uploads on every run.
2. **S3 error classes lack an explicit `retryable` attribute** — the `worker-process-management`
   spec requires all errors to carry an explicit `retryable: bool`; `S3Error` and `S3UploadError`
   rely on a `getattr` default instead.
3. **Cancelled job detected after RUNNING transition** — the spec requires detecting CANCELLED
   status before setting the job to RUNNING; the current implementation sets RUNNING first, then
   checks.

## Scope

**In scope:**

- Implement S3 hash-based dedup: query for a prior `ConversionOutput` with a matching
  `markdown_hash_xx64` before uploading; reuse existing S3 keys when found
- Add `retryable: ClassVar[bool] = True` (or `False`) to `S3Error`, `S3UploadError`, and any
  other error classes in `s3_client.py` that lack the attribute
- Move the CANCELLED check in `_initialize_running_job()` to occur before the RUNNING state
  transition

**Out of scope:**

- Changes to artifact layout or S3 key structure
- Changes to the idempotency key computation
- New retry policies or backoff behaviour
- Any other spec or implementation changes

## Approach

### S3 hash dedup

Before uploading artifacts, query `ConversionOutput` for a record with the same `aizk_uuid` (or alternatively any record with the matching `markdown_hash_xx64`) produced by a prior succeeded job.
If found, create a new `ConversionOutput` pointing to the existing S3 keys without re-uploading, and transition the job to SUCCEEDED.
If not found, proceed with the current upload path.

### S3 error retryability

Add `retryable: ClassVar[bool] = True` to `S3Error` and `S3UploadError` in `src/aizk/conversion/storage/s3_client.py`.
This makes the attribute explicit and type-safe, consistent with the rest of the exception hierarchy.

### Cancel detection ordering

In `_initialize_running_job()`, read the current job status from the database first.
If the job is already CANCELLED, return False immediately without issuing the UPDATE to RUNNING.
