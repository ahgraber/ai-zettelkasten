# Tasks: Docling Config Clarity

## Config rename

- [x] In `config.py`: rename field `chat_completions_base_url` → `docling_picture_description_base_url` (env alias `DOCLING_PICTURE_DESCRIPTION_BASE_URL`)
- [x] In `config.py`: rename field `chat_completions_api_key` → `docling_picture_description_api_key` (env alias `DOCLING_PICTURE_DESCRIPTION_API_KEY`)
- [x] In `config.py`: rename field `docling_vlm_model` → `docling_picture_description_model` (env alias `DOCLING_PICTURE_DESCRIPTION_MODEL`)
- [x] In `config.py`: rename validator `validate_chat_completions_fields` → `validate_picture_description_fields` and update field references inside it
- [x] In `config.py`: update `is_picture_description_enabled()` to reference new field names

## Converter update

- [x] In `converter.py`: update all references to `chat_completions_base_url`, `chat_completions_api_key`, and `docling_vlm_model` to the new field names
- [x] In `converter.py`: update the log string in `_get_picture_description_options` from `"CHAT_COMPLETIONS_BASE_URL or CHAT_COMPLETIONS_API_KEY not set"` to `"DOCLING_PICTURE_DESCRIPTION_BASE_URL or DOCLING_PICTURE_DESCRIPTION_API_KEY not set"`

## Startup probe

- [x] In `startup.py`: add `probe_picture_description(config)` — `GET {base_url}/models` with `Authorization: Bearer` header, 10s timeout; raises `StartupValidationError` on non-2xx or connection error; no-op if base_url or api_key is unset
- [x] In `startup.py`: call `probe_picture_description(config)` from `validate_startup()` after `probe_karakeep()`
- [x] In `startup.py`: update `log_feature_summary()` reason string from `"chat completions endpoint not configured"` to `"DOCLING_PICTURE_DESCRIPTION_BASE_URL not configured"`
- [x] In `startup.py`: update the picture classification disabled-reason check to reference new field names

## Health check

- [x] In `health.py`: add `_check_picture_description(config)` — `GET {base_url}/models` with `Authorization: Bearer` header, 5s timeout; returns `CheckResult(name="picture_description", ...)`
- [x] In `health.py`: in `readiness()`, conditionally include `_check_picture_description(config)` when `config.is_picture_description_enabled()`

## Manifest

- [x] In `manifest.py` (or wherever the config snapshot is built): update field reference from `docling_vlm_model` → `docling_picture_description_model`
- [x] Verify `ManifestConfigSnapshot` (if it has `extra="forbid"`) is updated to use the new field name

## `.env.example` rewrite

- [x] Rewrite `.env.example` with the following sections, in order:
  - `# --- Core ---` (DATABASE_URL)
  - `# --- S3 ---` (S3_BUCKET_NAME, S3_REGION, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_ENDPOINT_URL)
  - `# --- KaraKeep ---` (KARAKEEP_BASE_URL, KARAKEEP_API_KEY) — fix `KARAKEEP_API_URL` typo
  - `# --- Worker ---` (WORKER_CONCURRENCY, WORKER_GPU_CONCURRENCY, QUEUE_MAX_DEPTH, QUEUE_RETRY_AFTER_SECONDS, FETCH_TIMEOUT_SECONDS, RETRY_MAX_ATTEMPTS, RETRY_BASE_DELAY_SECONDS, WORKER_STALE_JOB_MINUTES, WORKER_JOB_TIMEOUT_SECONDS, WORKER_DRAIN_TIMEOUT_SECONDS)
  - `# --- Docling Pipeline ---` (DOCLING_PDF_MAX_PAGES, DOCLING_ENABLE_OCR, DOCLING_ENABLE_TABLE_STRUCTURE)
  - `# --- Docling Picture Description ---` (with OpenRouter + local vLLM provider examples; DOCLING_PICTURE_DESCRIPTION_BASE_URL, DOCLING_PICTURE_DESCRIPTION_API_KEY, DOCLING_PICTURE_DESCRIPTION_MODEL, DOCLING_PICTURE_TIMEOUT, DOCLING_ENABLE_PICTURE_CLASSIFICATION)
  - `# --- API Server ---` (API_HOST, API_PORT, API_RELOAD)
  - `# --- Logging ---` (LOG_LEVEL, LOG_FORMAT)
  - `# --- Observability ---` (MLFLOW_TRACING_ENABLED, MLFLOW_TRACKING_URI, MLFLOW_EXPERIMENT_NAME)
  - `# --- Litestream ---` (all LITESTREAM\_\* fields)
- [x] Remove the `_UNDERSCORE_PREFIXED` provider preset block and `CHAT_COMPLETIONS_*` lines

## Tests

- [x] Update all `monkeypatch.setenv("CHAT_COMPLETIONS_BASE_URL", ...)` → `DOCLING_PICTURE_DESCRIPTION_BASE_URL` in test files
- [x] Update all `monkeypatch.setenv("CHAT_COMPLETIONS_API_KEY", ...)` → `DOCLING_PICTURE_DESCRIPTION_API_KEY` in test files
- [x] Update all `monkeypatch.setenv("DOCLING_VLM_MODEL", ...)` → `DOCLING_PICTURE_DESCRIPTION_MODEL` in test files
- [x] Add unit tests for `probe_picture_description`: reachable (200), non-2xx, connection error, not-configured no-op
- [x] Add unit tests for `_check_picture_description`: ok, unavailable, omitted when not configured
- [x] Update `test_startup.py` log string assertions to match new reason string
- [x] Update manifest/config snapshot contract tests to use `docling_picture_description_model`
