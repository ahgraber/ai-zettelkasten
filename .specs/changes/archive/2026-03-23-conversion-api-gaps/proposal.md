# Proposal: Conversion API Gaps

## Intent

Code analysis of `aizk/conversion/api/` against the baseline conversion-api spec revealed three unimplemented requirements: a dedicated bookmark outputs endpoint, the attempt-count increment on retry, and content-serving endpoints for conversion artifacts.
Together these gaps mean clients cannot retrieve a bookmark's conversion history, retry semantics were violated at runtime, and there is no way to access the actual converted content (markdown, manifest, figures) without direct S3 access.

## Scope

**In scope:**

- `GET /v1/bookmarks/{aizk_uuid}/outputs` — return all conversion output records for a bookmark ordered by creation time descending, with an optional `latest` query flag _(implemented)_
- Fix `_apply_job_retry` to increment `job.attempts` on each retry _(implemented)_
- `GET /v1/outputs/{output_id}/manifest` — raw manifest JSON passthrough from S3
- `GET /v1/outputs/{output_id}/markdown` — proxied markdown text from S3
- `GET /v1/outputs/{output_id}/figures/{filename}` — proxied figure image from S3
- `S3NotFoundError` exception class and `get_object_bytes` method on `S3Client`
- Replace `get_s3_client` in `dependencies.py` to inject `S3Client` (currently returns an unused raw boto3 client)

**Out of scope:**

- `POST /v1/jobs/batch` — batch job submission; deferred as future work
- Exposing the existing delete capability (`_apply_job_delete`) via a route
- Aligning the cancel/retry status allowlists with the spec (CANCELLED retry, FAILED_RETRYABLE cancel are intentional extensions)
- Presigned URL redirects for content (deferred; revisit if memory pressure becomes a concern)
- Streaming responses for large content
- Changes to worker logic or storage

## Approach

- Increment `job.attempts` in `_apply_job_retry` before status reset _(done)_
- Add `OutputResponse` schema and `GET /v1/bookmarks/{aizk_uuid}/outputs` route _(done)_
- Add `S3NotFoundError` and `get_object_bytes` to `S3Client`
- Replace `get_s3_client` in `dependencies.py` to return `S3Client`
- Add new `outputs` router at `/v1/outputs` with manifest, markdown, and figures endpoints
- Figure keys constructed deterministically as `{s3_prefix}/figures/{filename}`; filenames validated to reject path traversal
