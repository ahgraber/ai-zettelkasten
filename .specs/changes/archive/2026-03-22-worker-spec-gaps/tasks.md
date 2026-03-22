# Tasks: worker-spec-gaps

## S3 hash dedup

- [x] In `src/aizk/conversion/workers/worker.py`, before entering the S3 upload loop, query
  `ConversionOutput` for the most recent succeeded output for the same `aizk_uuid`; if one exists
  and its `markdown_hash_xx64` matches the newly computed hash, skip upload and create a new
  output record pointing to the existing S3 keys, then transition the job to SUCCEEDED
- [x] Add a unit test verifying that a matching hash causes upload to be skipped and a new output
  record is created with the existing S3 keys
- [x] Add a unit test verifying that a differing hash proceeds with the full upload path

## S3 error retryability

- [x] In `src/aizk/conversion/storage/s3_client.py`, add `retryable: ClassVar[bool] = True` to
  `S3Error` and `S3UploadError`; import `ClassVar` from `typing`
- [x] Verify existing tests for `handle_job_error()` with S3 errors still pass; add a test
  asserting `S3UploadError.retryable is True` explicitly

## Cancel detection ordering

- [x] In `src/aizk/conversion/workers/worker.py`, in the job-initialisation function, read the
  current job status from the database and return False immediately if it is CANCELLED, before
  issuing the UPDATE to set status RUNNING
- [x] Add or update a unit test asserting that a CANCELLED job is not transitioned to RUNNING and
  no subprocess is started

## Spec sync

- [x] After implementation is verified, merge delta specs into baseline:
  - `.specs/specs/conversion-worker/spec.md` — update "Skip S3 overwrite when content hash
    matches" requirement to remove the unimplemented note and add the "No prior output" scenario
  - `.specs/specs/worker-process-management/spec.md` — update both MODIFIED requirements to
    reflect the now-correct ordering and explicit attribute
