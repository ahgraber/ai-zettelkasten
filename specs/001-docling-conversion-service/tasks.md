# Tasks: Docling Conversion Service

**Input**: Design documents from `/specs/001-docling-conversion-service/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/openapi.yaml

**Tests**: Tests are required; each user story includes contract/integration/unit test tasks to follow TDD.

**Constitution Alignment**: Tasks include data provenance tracking (manifest.json, conversion metadata), reproducible configs (pinned Docling versions, payload versioning), privacy (no PII, env var secrets), and observability (structured logging, metrics).

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3, US4)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [x] T001 Create project structure: shared `src/aizk/datamodel/` (bookmark.py, job.py, output.py, \_\_init\_\_.py), shared `src/aizk/db.py` utilities, and feature `src/aizk/conversion/` (api/, workers/, storage/, utilities/, templates/)
- [x] T002 Create tests/conversion/ structure with subdirs (unit/, integration/, contract/)
- [x] T003 [P] Add pyproject.toml dependencies: fastapi, docling>=2.65.0, sqlmodel, boto3, xxhash, httpx, pydantic-settings, uvicorn
- [x] T004 [P] Create .env.example with template environment variables (S3 credentials, database path, worker concurrency)
- [x] T005 [P] Ensure .env is in .gitignore; configuration reads from environment variables only

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T006 Create SQLModel database models: Bookmark entity in src/aizk/datamodel/bookmark.py with fields (id, karakeep_id, aizk_uuid, url, normalized_url, title, content_type, source_type, created_at, updated_at)
- [x] T007 [P] Create SQLModel database models: ConversionJob entity in src/aizk/datamodel/job.py with fields (id, aizk_uuid, payload_version, status, attempts, error_code, error_message, idempotency_key, next_attempt_at, last_error_at, queued_at, started_at, finished_at, created_at, updated_at) and status enum
- [x] T008 [P] Create SQLModel database models: ConversionOutput entity in src/aizk/datamodel/output.py with fields (id, job_id, aizk_uuid, payload_version, s3_prefix, markdown_key, manifest_key, markdown_hash_xx64, figure_count, docling_version, pipeline_name, created_at)
- [x] T009 Create shared DB utilities in src/aizk/db.py: get_engine() configured to support concurrent read access from multiple workers with transaction isolation (see research.md ADR-003 for specific PRAGMA recommendations), get_session(), create_db_and_tables()
- [x] T010 Ensure indexes are declared and loaded by metadata in src/aizk/datamodel/\_\_init\_\_.py (imports models to populate SQLModel.metadata)
- [x] T011 Implement URL normalization utility in src/aizk/utilities/url_utils.py: normalize_url(url) lowercases domain, sorts query params, removes fragments
- [x] T012 [P] Implement bookmark validation utility in src/aizk/conversion/utilities/bookmark_utils.py: validate_bookmark_content(bookmark) validates KaraKeep bookmark has HTML content, text, or PDF asset; raises exception with error_code='missing_content' if all absent. Implement content type detection: detect_content_type(url, karakeep_metadata) returns 'html' or 'pdf' based on metadata or URL suffix.
- [x] T012a [P] Implement source type detection utility in src/aizk/conversion/utilities/bookmark_utils.py: detect_source_type(url) returns 'arxiv', 'github', or 'other' based on URL domain/pattern (NOT content format)
- [x] T013 [P] Implement idempotency key computation in src/aizk/conversion/utilities/hashing.py: compute_idempotency_key(aizk_uuid, payload_version, docling_version, config_hash) returns SHA256 hex digest
- [x] T014 [P] Implement markdown hash computation in src/aizk/conversion/utilities/hashing.py: compute_markdown_hash(markdown_text) returns xxHash64 hex digest of normalized markdown (UTF-8, LF line endings, trimmed)
- [x] T015 [P] ~~Implement filename normalization in src/aizk/conversion/utilities/filename_utils.py: normalize_filename(title) lowercases, replaces special chars with hyphens, strips leading/trailing dots/dashes, truncates to 200 chars~~ override: filename normalization already exists in utilities.file_utils
- [x] T016 Create configuration management in src/aizk/conversion/utilities/config.py using pydantic-settings: ConversionConfig with fields for S3 credentials, database path, worker concurrency, fetch timeouts, temp workspace path
- [x] T017 Create FastAPI app setup in src/aizk/conversion/api/main.py with lifespan context manager for database initialization and cleanup
- [x] T018 [P] Create FastAPI dependency injection in src/aizk/conversion/api/dependencies.py: wire DB session via `aizk.db.get_session()` and provide get_s3_client() dependency
- [x] T019 [P] Create structured logging configuration in src/aizk/conversion/utilities/logging.py with context fields (aizk_uuid, job_id, karakeep_id, status)
- [x] T020 Create CLI entrypoint in src/aizk/conversion/cli.py with commands: db-init (initialize database via aizk.db.create_db_and_tables), serve (run FastAPI server), worker (run background worker)
- [x] T020a [P] Implement process role identification (via setproctitle): API server identifies itself as 'api' role; worker processes identify as 'worker' role; CLI entrypoints identify as 'cli' role. Roles must be visible to system operators (e.g., via process title, environment variable, or telemetry).

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - Submit Bookmark for Conversion (Priority: P1) 🎯 MVP

**Goal**: Users can submit KaraKeep bookmarks via API and receive Markdown documents with extracted figures in S3

**Independent Test**: Submit bookmark URL via POST /v1/jobs, wait for job completion, verify Markdown and figures in S3 with correct structure

### Tests for User Story 1 (write first)

- [x] T021 [P] [US1] Contract test for POST /v1/jobs and GET /v1/jobs in tests/conversion/contract/test_jobs_api.py (validate OpenAPI schema compliance)
- [x] T021a [P] [US1] Unit tests for bookmark validation in tests/conversion/unit/test_bookmark_utils.py (validate_bookmark_content, detect_content_type with KaraKeep bookmark objects)
- [x] T022 [P] [US1] Unit tests for utilities in tests/conversion/unit/test_url_utils.py (normalize_url, detect_source_type, get_arxiv_id, standardize_github)
- [x] T023 [P] [US1] Unit tests for hashing and filename utils in tests/conversion/unit/test_hashing.py and tests/utilities/test_file_utils.py (compute_idempotency_key, compute_markdown_hash, normalize_filename)
- [x] T024 [US1] Integration test for end-to-end conversion in tests/conversion/integration/test_conversion_flow.py (submit job → worker processes → outputs stored in S3-compatible storage)

### Implementation for User Story 1

- [x] T025 [P] Implement arXiv ID extraction in src/aizk/utilities/arxiv_utils.py: get_arxiv_id(url) using regex pattern
- [x] T026 [P] Implement GitHub URL normalization in src/aizk/utilities/url_utils.py: standardize_github(url) for repo-root URLs
- [x] T027 Implement fetch_karakeep_pdf_asset(karakeep_id, asset_id) using karakeep_client to fetch PDF asset bytes when not provided in submission.
- [ ] T028 Implement arXiv content handler in src/aizk/conversion/workers/fetcher.py: fetch_arxiv_content(bookmark, pdf_asset_bytes=None) handles three cases: (1) if bookmark source URL is from abstract page (arxiv.org/abs), resolve `arxiv_id` using aizk.utilities.arxiv_utils and download PDF using `AsyncArxivClient.download_paper_pdf(arxiv_id)`; (2) if bookmark has PDF asset, use provided pdf_asset_bytes or fetch from KaraKeep if None; (3) if bookmark has HTML content, resolve `arxiv_id` (use `arxiv_pdf_url` when present) and download via client. Returns PDF bytes for conversion.
- [x] T029 [P] Implement GitHub README fetcher in src/aizk/conversion/workers/fetcher.py: fetch_github_readme(owner, repo) tries README.md, README.rst, README.txt, README in main/master branches
- [x] T030 Implement Docling HTML pipeline in src/aizk/conversion/workers/converter.py: convert_html(html_bytes, temp_dir) returns markdown_text and list of figure paths
- [x] T031 [P] Implement Docling PDF pipeline in src/aizk/conversion/workers/converter.py: convert_pdf(pdf_bytes, temp_dir) returns markdown_text and list of figure paths
- [x] T032 Implement S3 upload in src/aizk/conversion/storage/s3_client.py: upload_file(local_path, s3_key) with verification (check ETag or HTTP 200)
- [x] T033 [P] Implement S3 batch upload in src/aizk/conversion/storage/s3_client.py: upload_artifacts(temp_dir, s3_prefix) uploads markdown, figures, and manifest; returns list of uploaded keys
- [x] T034 [P] Implement manifest generation in src/aizk/conversion/storage/manifest.py: generate_manifest(bookmark, job, artifacts) creates manifest.json dict with version, source, conversion, artifacts sections per data-model.md schema. **Manifest must store absolute S3 URIs** (s3://bucket/aizk_uuid/filename) for all artifact keys (markdown, figures) to ensure durability and portability. ConversionOutput.manifest_key must store full S3 URI or absolute path, not just prefix.
- [ ] T035 Implement worker main loop in src/aizk/conversion/workers/worker.py: poll_and_process_jobs() queries jobs with status=QUEUED ordered by queued_at, picks up one job, transitions to RUNNING
- [ ] T036 Implement job processing in src/aizk/conversion/workers/worker.py: process_job(job_id) orchestrates fetch → convert → upload → create output record → mark SUCCEEDED with two-phase transaction. **Phase 1 (Conversion)**: Begin transaction, execute fetch & convert, store converted artifacts in temp dir, transition to UPLOAD_PENDING, commit. **Phase 2 (Upload)**: Begin new transaction, execute S3 upload & verify (via ETag/HEAD check), create output record, mark SUCCEEDED, commit. **Resilience**: If Phase 1 succeeds but Phase 2 fails, job remains in UPLOAD_PENDING with artifacts cached; retry queries UPLOAD_PENDING jobs and re-executes Phase 2 only (no reconversion). If Phase 1 fails, transition to FAILED_RETRYABLE for full retry. This enables efficient S3 retry without wasted conversion compute.
- [ ] T037 Implement error handling in src/aizk/conversion/workers/worker.py: handle_job_error(job_id, error) determines FAILED_RETRYABLE vs FAILED_PERM based on error_code, computes next_attempt_at with exponential backoff, increments attempts
- [ ] T037a Implement upload retry handler in src/aizk/conversion/workers/worker.py: process_upload_pending_jobs() queries jobs with status=UPLOAD_PENDING ordered by last_error_at, retrieves cached conversion artifacts from temp workspace, re-executes Phase 2 (S3 upload & verify) without reconverting. Falls back to full retry (FAILED_RETRYABLE) if artifacts missing.
- [ ] T038 Implement Pydantic request schema in src/aizk/conversion/api/schemas/jobs.py: JobSubmission with fields (karakeep_id, url, title, payload_version, idempotency_key optional)
- [ ] T039 [P] Implement Pydantic response schema in src/aizk/conversion/api/schemas/jobs.py: JobResponse with fields from ConversionJob model
- [ ] T040 Implement POST /v1/jobs endpoint in src/aizk/conversion/api/routes/jobs.py: validate bookmark has required content (HTML/text/PDF) using bookmark_utils.validate_bookmark_content(), create or lookup bookmark record, compute idempotency_key, check for duplicate, create ConversionJob with status=NEW→QUEUED, return 201 or 200 with existing job. Accept KaraKeep bookmark object and optional pdf_asset_bytes in request.
- [ ] T041 Implement GET /v1/jobs/{job_id} endpoint in src/aizk/conversion/api/routes/jobs.py: query job by ID, return JobResponse or 404
- [ ] T042 [P] Implement GET /v1/jobs endpoint in src/aizk/conversion/api/routes/jobs.py: query jobs with filters (status, aizk_uuid, karakeep_id, created_after, created_before), pagination (limit, offset), return JobList
- [ ] T043 Register routes in src/aizk/conversion/api/main.py: include jobs router with /v1 prefix

**Checkpoint**: User Story 1 complete - bookmark submission and conversion working end-to-end

---

## Phase 4: User Story 2 - Monitor and Retry Failed Jobs (Priority: P2)

**Goal**: Users can view jobs in Web UI, identify failed jobs, and retry them individually or in batch

**Independent Test**: Access /ui/jobs, view job list with status filters, select failed jobs, click Retry button, verify jobs reset to QUEUED

### Tests for User Story 2 (write first)

- [ ] T044 [P] [US2] Contract test for POST /v1/jobs/{job_id}/retry and /cancel in tests/conversion/contract/test_jobs_retry_cancel.py
- [ ] T045 [P] [US2] Integration test for bulk actions in tests/conversion/integration/test_jobs_actions.py (retry/cancel flows)
- [ ] T046 [US2] Integration test for /ui/jobs rendering in tests/conversion/integration/test_ui_jobs.py (table columns, filters, bulk action form)

### Implementation for User Story 2

- [ ] T047 Implement POST /v1/jobs/{job_id}/retry endpoint in src/aizk/conversion/api/routes/jobs.py: reset status to QUEUED for FAILED_RETRYABLE/FAILED_PERM/CANCELLED jobs, increment attempts, clear next_attempt_at, return JobResponse or 400/404
- [ ] T048 [P] Implement POST /v1/jobs/{job_id}/cancel endpoint in src/aizk/conversion/api/routes/jobs.py: mark QUEUED→CANCELLED immediately, mark RUNNING→CANCELLED (worker checks periodically), return JobResponse or 400/404
- [ ] T049 [P] Implement POST /v1/jobs/actions endpoint in src/aizk/conversion/api/routes/jobs.py: bulk retry or cancel for array of job_ids, return BulkActionResponse with per-job results
- [ ] T050 Create Jinja2 base template in src/aizk/conversion/templates/base.html with HTML structure, CSS for table styling, JavaScript for client-side filtering and checkbox selection
- [ ] T051 Create Jinja2 jobs table template in src/aizk/conversion/templates/jobs.html: displays jobs with columns (Job ID, aizk_uuid, karakeep_id, title, status, attempts, queued_at, started_at, finished_at, error_code), includes checkboxes, Retry/Cancel buttons, status/text filters
- [ ] T052 Implement GET /ui/jobs endpoint in src/aizk/conversion/api/routes/ui.py: query all jobs, render jobs.html template with job list
- [ ] T053 Register UI routes in src/aizk/conversion/api/main.py: include ui router with /ui prefix, configure Jinja2Templates

**Checkpoint**: User Story 2 complete - Web UI operational monitoring and retry working

---

## Phase 5: User Story 3 - Reprocess Bookmark with Pipeline Upgrade (Priority: P3)

**Goal**: Users can force reprocessing of bookmarks with new payload_version after Docling upgrades, with content-based deduplication

**Independent Test**: Submit same bookmark with payload_version=2, verify new job created despite existing output, compare markdown_hash_xx64 between versions

### Tests for User Story 3 (write first)

- [ ] T054 [P] [US3] Contract test for GET /v1/outputs/{aizk_uuid} in tests/conversion/contract/test_outputs_api.py
- [ ] T055 [US3] Integration test for reprocessing flow in tests/conversion/integration/test_reprocess_flow.py (payload_version increment, hash comparison, reuse vs overwrite)

### Implementation for User Story 3

- [ ] T056 Implement Pydantic response schema in src/aizk/conversion/api/schemas/outputs.py: ConversionOutput with fields from ConversionOutput model
- [ ] T057 Implement GET /v1/outputs/{aizk_uuid} endpoint in src/aizk/conversion/api/routes/outputs.py: query conversion_outputs by aizk_uuid ordered by created_at descending, support ?latest=true query parameter, return output list or 404
- [ ] T058 Update process_job in src/aizk/conversion/workers/worker.py: after computing markdown_hash_xx64, query most recent conversion_outputs for same aizk_uuid; if hash matches, reuse existing S3 location without overwriting; if hash differs, upload new artifacts
- [ ] T059 Update POST /v1/jobs in src/aizk/conversion/api/routes/jobs.py: allow submissions with incremented payload_version despite existing outputs, compute new idempotency_key based on new payload_version
- [ ] T060 Register outputs routes in src/aizk/conversion/api/main.py: include outputs router with /v1 prefix

**Checkpoint**: User Story 3 complete - reprocessing with payload versioning and deduplication working

---

## Phase 6: User Story 4 - Batch Submission with Backpressure Handling (Priority: P3)

**Goal**: Manager components can submit batches of bookmarks efficiently with per-item status reporting

**Independent Test**: Submit batch of 20 jobs via POST /v1/jobs/batch, observe per-item results with validation errors for invalid entries

### Tests for User Story 4 (write first)

- [ ] T061 [P] [US4] Contract test for POST /v1/jobs/batch in tests/conversion/contract/test_jobs_batch.py (per-item status handling)
- [ ] T062 [US4] Integration test for batch submission in tests/conversion/integration/test_batch_submission.py (mixed valid/invalid jobs, duplicate idempotency_keys reuse existing job)

### Implementation for User Story 4

- [ ] T063 Implement Pydantic batch response schema in src/aizk/conversion/api/schemas/jobs.py: BatchJobResponse with per-item results (job_id or error details)
- [ ] T064 Implement POST /v1/jobs/batch endpoint in src/aizk/conversion/api/routes/jobs.py: process array of JobSubmission items independently, validate each, create jobs for valid entries, return 207 Multi-Status with BatchJobResponse containing per-item success/failure details
- [ ] T065 Update POST /v1/jobs/batch in src/aizk/conversion/api/routes/jobs.py: handle duplicate idempotency_keys gracefully, return existing job details without creating new records

**Checkpoint**: User Story 4 complete - batch submission working with per-item status

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [ ] T066 [P] Implement GET /health endpoint in src/aizk/conversion/api/routes/health.py: return service health status with database connectivity check
- [ ] T067 [P] Add metrics emission in src/aizk/conversion/workers/worker.py: queue depth (jobs with status=QUEUED count), job duration histogram (finished_at - started_at), job status counts (SUCCEEDED/FAILED/CANCELLED)
- [ ] T068 [P] Add metrics emission in src/aizk/conversion/workers/fetcher.py: fetch latency histogram, S3 upload latency histogram
- [ ] T069 Add logging throughout all modules: aizk_uuid, job_id, karakeep_id, status in all log messages; use structured logging format
- [ ] T070 Create Docker Compose configuration in docker-compose.yml: conversion-service container (FastAPI + workers), s3 service container (S3-compatible storage), volume mounts for SQLite and temp workspace
- [ ] T071 [P] Create README documentation in specs/001-docling-conversion-service/: link to quickstart.md, spec.md, and data-model.md
- [ ] T072 Update CHANGELOG.md per Keep a Changelog format: Add section "## [0.1.0] - 2025-12-23" with "### Added" entries for all user stories, following Conventional Commits with "feat(conversion):" prefix
- [ ] T073 Bump version in pyproject.toml from 0.0.1 → 0.1.0 per Semantic Versioning (MINOR bump for new feature)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Story 1 (Phase 3)**: Depends on Foundational phase completion
- **User Story 2 (Phase 4)**: Depends on Foundational phase completion (US1 provides base job management)
- **User Story 3 (Phase 5)**: Depends on US1 completion (extends job submission and output management)
- **User Story 4 (Phase 6)**: Depends on US1 completion (extends job submission with batch endpoint)
- **Polish (Phase 7)**: Depends on all user stories being complete

### User Story Dependencies

- **User Story 1 (P1)**: Core conversion functionality - MUST be implemented first
- **User Story 2 (P2)**: Can start after US1 (depends on job management endpoints from US1)
- **User Story 3 (P3)**: Can start after US1 (depends on job submission and output retrieval from US1)
- **User Story 4 (P3)**: Can start after US1 (depends on job submission from US1)

**Note**: US2, US3, US4 could theoretically be implemented in parallel after US1, but they have logical dependencies on US1's job management infrastructure.

### Parallel Opportunities

**Within Setup (Phase 1)**:

- T003 (dependencies), T004 (.env template), T005 (gitignore) can run in parallel

**Within Foundational (Phase 2)**:

- T007 (ConversionJob model), T008 (ConversionOutput model) can run in parallel after T006 (Bookmark model)
- T012 (source type detection), T013 (idempotency key), T014 (markdown hash), T015 (filename normalization) can all run in parallel
- T018 (dependencies), T019 (logging config) can run in parallel after T016 (config) and T017 (FastAPI app)

**Within User Story 1 (Phase 3)**:

- T025 (arXiv extraction), T026 (GitHub extraction) can run in parallel
- T030 (HTML pipeline), T031 (PDF pipeline) can run in parallel after T027 (fetcher)
- T033 (S3 batch upload), T034 (manifest generation) can run in parallel after T032 (S3 upload)
- T039 (response schema) can run in parallel after T038 (request schema)
- T042 (GET /v1/jobs) can run in parallel after T040 (POST /v1/jobs)

**Within User Story 2 (Phase 4)**:

- T048 (cancel endpoint), T049 (bulk actions) can run in parallel after T047 (retry endpoint)

**Within Polish (Phase 7)**:

- T066 (health endpoint), T067 (worker metrics), T068 (fetcher metrics), T071 (README) can all run in parallel

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T005)
2. Complete Phase 2: Foundational (T006-T020) - **CRITICAL BLOCKING PHASE**
3. Complete Phase 3: User Story 1 (T021-T039)
4. **STOP and VALIDATE**: Test bookmark submission → conversion → S3 output end-to-end
5. Deploy/demo MVP

### Incremental Delivery

1. **Foundation** (Phases 1-2): Database models, utilities, FastAPI setup → Infrastructure ready
2. **MVP** (Phase 3): Core conversion functionality → Users can convert bookmarks via API
3. **Monitoring** (Phase 4): Web UI for operational visibility → Operators can monitor and retry jobs
4. **Reprocessing** (Phase 5): Payload versioning → Users can reprocess with Docling upgrades
5. **Batch Operations** (Phase 6): Bulk submission → Efficient batch processing for managers
6. **Production Ready** (Phase 7): Metrics, health checks, Docker deployment → Ready for production

### Parallel Team Strategy

With multiple developers:

1. **Together**: Complete Setup + Foundational (Phases 1-2)
2. **After Foundational**:
   - Developer A: User Story 1 (Phase 3) - MUST complete first
   - Developer B: Documentation and polish setup (parts of Phase 7)
3. **After US1 Complete**:
   - Developer A: User Story 2 (Phase 4)
   - Developer B: User Story 3 (Phase 5)
   - Developer C: User Story 4 (Phase 6)
4. **Final**: All developers collaborate on remaining polish tasks

---

## Notes

- Tasks follow strict checklist format: `- [ ] [ID] [P?] [Story?] Description with file path`
- [P] tasks can run in parallel (different files, no blocking dependencies)
- [Story] labels (US1, US2, US3, US4) map to user stories from spec.md
- Each user story delivers independent value and can be tested separately
- Tests omitted per feature specification (not explicitly requested); TDD approach during implementation as needed
- Constitution alignment: data provenance (manifest.json), reproducibility (payload versioning), privacy (env vars), observability (structured logging, metrics)
- Foundational phase (Phase 2) BLOCKS all user story work - must complete first
- MVP = Phases 1-3 only (Setup + Foundational + User Story 1)
