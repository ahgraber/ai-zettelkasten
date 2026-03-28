# Tasks: Conversion API Gaps

## Retry fix

- [x] Increment `job.attempts` in `_apply_job_retry` before resetting status (`src/aizk/conversion/api/routes/jobs.py`)
- [x] Add/update test asserting that `attempts` is incremented on retry

## Bookmark outputs endpoint

- [x] Add `OutputResponse` schema exposing `ConversionOutput` fields to `src/aizk/conversion/api/schemas/jobs.py`
- [x] Add `GET /v1/bookmarks/{aizk_uuid}/outputs` route to a new or existing router, accepting `?latest=true` query param
- [x] Add tests: multiple outputs returned in descending order, `latest` returns only the newest

## S3Client read support

- [x] Add `S3NotFoundError(S3Error)` with `retryable = False` and constructor `(self, key: str)` calling `super().__init__(f"S3 object not found: {key}", "s3_not_found")` to `src/aizk/conversion/storage/s3_client.py`
- [x] Add `get_object_bytes(self, s3_key: str) -> bytes` to `S3Client`: fetch via boto3, raise `S3NotFoundError` on 404 `ClientError`, raise `S3Error` on other failures
- [x] Add unit tests for `get_object_bytes` to `tests/conversion/unit/test_s3_client.py`: success, `S3NotFoundError` on 404, `S3Error` on other error

## Output content endpoints

- [x] Replace `get_s3_client` in `src/aizk/conversion/api/dependencies.py` to instantiate and return an `S3Client` instead of a raw boto3 client
- [x] Create `src/aizk/conversion/api/routes/outputs.py` with router prefix `/v1/outputs`:
  - `GET /{output_id}/manifest` — DB lookup → fetch `manifest_key` → return raw bytes, `Content-Type: application/json`
  - `GET /{output_id}/markdown` — DB lookup → fetch `markdown_key` → return raw bytes, `Content-Type: text/markdown; charset=utf-8`
  - `GET /{output_id}/figures/{filename}` — validate filename (reject empty or containing `/` → 400) → DB lookup → construct key `{s3_prefix}/figures/{filename}` → fetch → return raw bytes with inferred image Content-Type
- [x] Export `outputs_router` from `src/aizk/conversion/api/routes/__init__.py`
- [x] Register `outputs_router` in `src/aizk/conversion/api/main.py`
- [x] Create `tests/conversion/integration/test_output_content.py`: correct bytes and Content-Type per endpoint; 404 on unknown output_id; 404 on `S3NotFoundError`; 502 on `S3Error`; 400 on filename containing `/` or `..`; 400 on empty filename; 404 for output with `figure_count == 0`
