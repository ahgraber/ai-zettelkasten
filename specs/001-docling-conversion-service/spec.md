# Feature Specification: Docling Conversion Service

**Feature Branch**: `001-docling-conversion-service`
**Created**: 2025-12-23
**Status**: Draft
**Input**: User description: "Convert KaraKeep bookmarks (HTML/PDF) to Markdown using Docling based on the demo in notebooks/docling_demo.py. Track bookmarks and conversion jobs in SQLite with durable state and idempotency. Run the conversion service remotely (container or Python server) via a FastAPI REST API. Deliver outputs to S3 at `bucket/<aizk_uuid>` with atomic finalization and reprocess support. Provide a simple HTML-only worker Web UI for monitoring and control (retry/cancel)."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Submit Bookmark for Conversion (Priority: P1)

A user submits a KaraKeep bookmark (HTML or PDF URL) to the conversion service and receives a Markdown document with extracted figures stored in S3.

**Why this priority**: This is the core value proposition - converting bookmarked content to searchable Markdown. Without this, the feature delivers no value.

**Independent Test**: Can be fully tested by submitting a single bookmark URL via API, waiting for job completion, and verifying Markdown and figures appear in S3 with correct structure. Delivers immediate value for content conversion.

**Acceptance Scenarios**:

1. **Given** a valid HTML bookmark URL, **When** user submits conversion job via API, **Then** system creates job record, fetches HTML, converts to Markdown with Docling, extracts figures, uploads to S3, and marks job as SUCCEEDED
2. **Given** a valid PDF bookmark URL, **When** user submits conversion job, **Then** system fetches PDF, converts to Markdown, extracts figures, uploads all artifacts to S3 under `bucket/<aizk_uuid>/`
3. **Given** a bookmark with identical idempotency_key already exists, **When** user resubmits same bookmark, **Then** system rejects submission with reason 'duplicate_idempotency_key'
4. **Given** an arXiv bookmark, **When** user submits arXiv URL, **Then** system extracts arxiv_id, prioritizes HTML version from export.arxiv.org, falls back to PDF if needed
5. **Given** a GitHub repository bookmark, **When** user submits GitHub URL, **Then** system extracts owner/repo, fetches README (md/rst/txt), converts to Markdown, uploads to S3
6. **Given** a completed conversion job, **When** system writes to S3, **Then** all artifacts are uploaded completely before job status is marked SUCCEEDED in database

---

### User Story 2 - Monitor and Retry Failed Jobs (Priority: P2)

A user views all conversion jobs in the Web UI, identifies failed jobs, and retries them individually or in batch.

**Why this priority**: Operational visibility and error recovery are essential for maintaining service reliability, but the core conversion must work first.

**Independent Test**: Can be tested by accessing Web UI at /ui/jobs, viewing job list with status filters, selecting failed jobs via checkboxes, and clicking Retry button. Delivers operational control independent of job submission.

**Acceptance Scenarios**:

1. **Given** user accesses /ui/jobs, **When** page loads, **Then** table displays all jobs with columns: Job ID, aizk_uuid, karakeep_id, title, status, attempts, timestamps, error_code
2. **Given** user views jobs with status=FAILED_RETRYABLE, **When** user selects one or more jobs and clicks Retry, **Then** system resets status to QUEUED, increments attempts, updates next_attempt_at
3. **Given** user views jobs with status=RUNNING, **When** user selects jobs and clicks Cancel, **Then** system attempts best-effort cancellation and updates status to CANCELLED
4. **Given** multiple jobs selected for bulk action, **When** user confirms action, **Then** system applies retry or cancel to all selected jobs and displays result summary
5. **Given** user filters by status, **When** user enters text search for aizk_uuid/karakeep_id/title, **Then** table updates to show only matching jobs

---

### User Story 3 - Reprocess Bookmark with Pipeline Upgrade (Priority: P3)

A user forces reprocessing of a previously converted bookmark after a Docling version upgrade or configuration change by submitting with a new payload_version.

**Why this priority**: Enables taking advantage of improved conversion quality over time, but is not essential for initial deployment.

**Independent Test**: Can be tested by submitting same bookmark with incremented payload_version, verifying new job created despite existing output, comparing markdown_hash_xx64 between versions. Delivers pipeline evolution capability independent of normal submissions.

**Acceptance Scenarios**:

1. **Given** a bookmark with successful conversion (payload_version=1), **When** user submits with payload_version=2, **Then** system creates new job despite existing output
2. **Given** new conversion completes, **When** markdown_hash_xx64 differs from previous output, **Then** system overwrites S3 artifacts and creates new conversion_outputs record
3. **Given** new conversion completes, **When** markdown_hash_xx64 matches previous output, **Then** system creates conversion_outputs record pointing to existing S3 location without overwriting
4. **Given** new conversion fails, **When** markdown is empty or invalid, **Then** system marks job FAILED_PERM and preserves existing S3 artifacts

---

### User Story 4 - Batch Submission with Backpressure Handling (Priority: P3)

A manager component submits batches of bookmarks to the service and gracefully handles backpressure when the queue is full.

**Why this priority**: Batch processing improves throughput for bulk imports, but single-job submission must work first.

**Independent Test**: Can be tested by submitting batch of jobs via POST /v1/jobs/batch, observing 429 responses when queue full, implementing exponential backoff. Delivers scalability independent of single submissions.

**Acceptance Scenarios**:

1. **Given** manager has 100 bookmarks to convert, **When** manager submits batch of 20 jobs, **Then** API processes each job independently and returns per-item results indicating which succeeded/failed validation
2. **Given** batch submission contains mix of valid and invalid jobs, **When** some jobs fail validation, **Then** API creates jobs for valid entries and returns detailed per-item status with error reasons for failed entries
3. **Given** batch submission includes duplicate idempotency_keys, **When** API processes batch, **Then** duplicates return existing job details without creating new records

## Technical Context & ADRs

- Default backend: FastAPI (services and APIs)
- Default storage: SQLite via SQLModel (local development/testing)
- Default Orchestration framework: Prefect - when needed
- **ADR Required**: S3 storage strategy and atomic finalization using database transactions
- **ADR Required**: Idempotency and payload_version semantics for reprocessing
- Secrets: Configuration and keys must be read from environment variables. Store them in a gitignored `.env` file locally; no secrets committed to the repo.

### Edge Cases

- What happens when a URL returns 404 or times out?
  - System marks job as FAILED_RETRYABLE, logs URL and HTTP status, schedules automatic retry with backoff
- What happens when Docling conversion produces empty Markdown?
  - System marks job as FAILED_PERM, preserves any existing successful output, logs warning with context
- How does system handle duplicate submissions with same idempotency_key?
  - API returns existing job details, does not create duplicate job
- What happens when S3 upload fails partway through?
  - System retries upload from beginning, does not mark job SUCCEEDED until all uploads verified
- How does system handle PDFs exceeding page limits?
  - System processes up to configured page limit, marks job SUCCEEDED with warning in manifest
- How does system handle simultaneous retry requests for the same job?
  - Database transaction ensures only one retry succeeds
- How does system handle GitHub README when repository has no README?
  - System marks job as FAILED_PERM
- What happens when S3 bucket is full or permissions denied?
  - System marks job FAILED_RETRYABLE, logs error details, retries with backoff

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST accept bookmark submissions via REST API with fields: aizk_uuid, karakeep_id, url, title, source_type, and optional payload_version, idempotency_key
- **FR-002**: System MUST normalize URLs for deduplication by removing fragments, sorting query parameters, and lowercasing domain
- **FR-003**: System MUST detect source_type from URL patterns: arxiv.org → 'arxiv', github.com → 'github', .pdf extension → 'pdf', otherwise → 'html'
- **FR-004**: System MUST assign or look up internal aizk_uuid for each bookmark and persist in bookmarks table with karakeep_id as unique key
- **FR-005**: System MUST create conversion_jobs record with status='NEW', compute idempotency_key from hash of aizk_uuid + payload_version + docling_version + config_hash, and reject submissions with duplicate idempotency_key
- **FR-006**: System MUST fetch source content with timeout (default: 30s), size cap (default: 50MB for HTML, 100MB for PDF), and retry logic (3 attempts with exponential backoff)
- **FR-007**: For arXiv sources, system MUST extract arxiv_id from URL using utilities, attempt HTML fetch from `export.arxiv.org/html/<arxiv_id>`, fall back to `export.arxiv.org/pdf/<arxiv_id>`
- **FR-008**: For GitHub sources, system MUST extract owner/repo from URL, fetch raw README content from default branch prioritizing README.md, then README.rst, then README
- **FR-009**: System MUST execute Docling conversion with appropriate pipeline (HTML or PDF) and extract figures to individual PNG files with sequential naming (figure1.png, figure2.png, ...)
- **FR-010**: System MUST compute xxhash64 of normalized Markdown content (UTF-8, LF line endings) and store in markdown_hash_xx64 field
- **FR-011**: System MUST write conversion outputs to isolated temp workspace at `<tmp_root>/<aizk_uuid>/<run_timestamp>/` including Markdown, figures, and manifest.json
- **FR-012**: System MUST upload all artifacts to S3 at `s3://<bucket>/<aizk_uuid>/` and verify successful upload before proceeding
- **FR-013**: System MUST compare new markdown_hash_xx64 with most recent conversion_outputs record; if hashes match, reuse existing S3 location and skip overwrite
- **FR-014**: System MUST create conversion_outputs record on successful conversion with fields: job_id, aizk_uuid, payload_version, s3_prefix, markdown_key, manifest_key, markdown_hash_xx64, markdown_bytes, figure_count, docling_version, pipeline_name, created_at
- **FR-015**: System MUST update conversion_jobs.status to SUCCEEDED in database transaction only after all S3 uploads complete and are verified, or FAILED_RETRYABLE/FAILED_PERM on error with error_code and error_message
- **FR-017**: System MUST expose GET /v1/jobs/{job_id} endpoint returning job status, timestamps, attempts, error details, and artifact summary if SUCCEEDED
- **FR-018**: System MUST expose GET /v1/jobs endpoint with filters for status, aizk_uuid, karakeep_id, and pagination support
- **FR-019**: System MUST expose POST /v1/jobs/{job_id}/retry endpoint that resets status to QUEUED, increments attempts, and clears next_attempt_at for FAILED_RETRYABLE or FAILED_PERM jobs
- **FR-020**: System MUST expose POST /v1/jobs/{job_id}/cancel endpoint that marks QUEUED or RUNNING jobs as CANCELLED on best-effort basis
- **FR-021**: System MUST expose POST /v1/jobs/batch endpoint accepting array of job submissions, processing each independently and returning per-item results with job_id or error details
- **FR-022**: System MUST expose POST /v1/jobs/actions endpoint accepting bulk retry or cancel operations with array of job IDs
- **FR-023**: System MUST expose GET /v1/outputs/{aizk_uuid} endpoint returning conversion_outputs records ordered by created_at descending; support ?latest=true query parameter to return only most recent output
- **FR-024**: System MUST render HTML-only Web UI at /ui/jobs displaying job table with columns: Job ID, aizk_uuid, karakeep_id, title, status, attempts, queued_at, started_at, finished_at, error_code
- **FR-025**: Web UI MUST provide checkboxes for multi-select, Retry and Cancel buttons posting to /v1/jobs/actions, and client-side filters for status and text search
- **FR-026**: System MUST process jobs with bounded concurrency (configurable, default: 4 parallel workers) in FIFO order by queued_at timestamp
- **FR-028**: System MUST use SQLite in WAL mode with synchronous=NORMAL, prepared statements, and single-writer pattern with database lock retry logic
- **FR-029**: System MUST create indexes: `idx_jobs_status_next_attempt` (conversion_jobs), `idx_bookmarks_normalized_url` (bookmarks), `idx_outputs_aizk_uuid` (conversion_outputs)
- **FR-030**: System MUST log key processing events with context identifiers (aizk_uuid, job_id, karakeep_id, status) enabling trace reconstruction
- **FR-031**: System MUST emit basic metrics: queue depth, job duration, job status counts, fetch latency, S3 upload latency
- **FR-032**: System MUST load configuration from environment variables with sensible defaults for local development
- **FR-033**: System MUST normalize Markdown filenames for cross-OS compatibility: lowercase, replace spaces/special chars with hyphens, strip leading/trailing dots/dashes, truncate to reasonable length
- **FR-034**: System MUST use payload_version equal to API version; idempotency_key computed as hash of aizk_uuid + payload_version + docling_version + config_hash

**Constitution Alignment**:

- **Data Provenance**: Store source metadata (url, normalized_url, karakeep_id, source_type) in bookmarks table; record docling_version, pipeline_name, payload_version in conversion_outputs; list all artifacts (figures, source files) in manifest.json
- **Reproducibility**: Pin Docling version in manifest.json; record fetch timestamps and content hashes (markdown_hash_xx64) for auditing
- **Privacy**: No user authentication required (internal-only); content fetched from public internet URLs in bookmarks; do not log S3 credentials or secrets
- **Observability**: Structured logging with aizk_uuid/job_id trace context; metrics for queue depth, latency, success/failure rates; optional trace_id propagation

### Key Entities

- **Bookmark**: Represents a KaraKeep bookmark with metadata needed for conversion execution. Attributes: id (PK), karakeep_id (unique), aizk_uuid (unique internal identifier), url (canonical source identifier), title, source_type (html/pdf/arxiv/github), normalized_url (for deduplication), created_at, updated_at. Relationships: one-to-many with conversion_jobs, one-to-many with conversion_outputs. Note: Does not replicate full KaraKeep bookmark; stores only fields required for conversion routing and deduplication. Source-specific identifiers (arxiv_id, github owner/repo) are extracted from URL during processing using utilities.
- **ConversionJob**: Represents a single conversion attempt. Attributes: id (PK), aizk_uuid (FK to bookmarks), payload_version, status (NEW/QUEUED/RUNNING/SUCCEEDED/FAILED_RETRYABLE/FAILED_PERM/CANCELLED), attempts, error_code, error_message, queued_at, started_at, finished_at, idempotency_key (unique), next_attempt_at (for retry backoff scheduling), last_error_at. Relationships: many-to-one with bookmarks, one-to-one with conversion_outputs (if SUCCEEDED)
- **ConversionOutput**: Represents successful conversion artifact set. Attributes: id (PK), job_id (FK to conversion_jobs), aizk_uuid (FK to bookmarks), payload_version, s3_prefix, markdown_key, manifest_key (contains full artifact listing), markdown_hash_xx64, markdown_bytes, figure_count, docling_version, pipeline_name, created_at. Relationships: many-to-one with bookmarks, one-to-one with conversion_jobs. Note: Individual figures and artifacts are listed in manifest.json only, not tracked as separate database rows.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users can submit a single bookmark and receive completed Markdown output in S3 within 90 seconds for HTML sources under 5MB
- **SC-002**: System processes PDF sources up to 20 pages and delivers Markdown output within 3 minutes with all figures extracted
- **SC-003**: System handles 4 concurrent conversion jobs without degradation or database lock errors
- **SC-004**: Duplicate submissions with identical idempotency_key are rejected with appropriate reason within 100ms (no redundant processing)
- **SC-005**: System maintains 99% idempotency correctness - no duplicate artifacts created for same logical job
- **SC-006**: Failed jobs marked FAILED_RETRYABLE are automatically retried up to 3 times with success rate above 70% on transient failures
- **SC-007**: Web UI page load at /ui/jobs completes within 2 seconds for job lists up to 1000 entries
- **SC-008**: Users can identify and retry failed jobs via Web UI with confirmation displayed within 5 seconds of action
- **SC-009**: Database transaction atomicity ensures 100% consumer reliability - consumers never receive S3 paths for incomplete conversions
- **SC-010**: Reprocessing with new payload_version after Docling upgrade detects content changes in 95% of cases via markdown_hash_xx64 comparison
- **SC-011**: System correctly prioritizes arXiv HTML over PDF in 100% of arXiv submissions, with fallback working when HTML unavailable
- **SC-012**: GitHub README fetching succeeds for 90% of public repositories with standard README naming conventions
- **SC-013**: Structured logs include aizk_uuid and job_id in 100% of processing events, enabling complete trace reconstruction
- **SC-014**: Idempotency protection prevents duplicate job creation in 100% of cases when same bookmark submitted multiple times with identical parameters
- **SC-015**: Markdown filename normalization produces valid cross-OS filenames for common title patterns

## Assumptions

- KaraKeep bookmark data includes URL, title, and karakeep_id at minimum; other fields may be optional or derived
- Docling library is available as Python package with stable API for HTML and PDF conversion pipelines
- S3-compatible storage is accessible with credentials provided via environment variables (supports AWS S3, Backblaze B2, Garage, MinIO, etc.)
- Internal-only deployment behind private network or localhost; no internet-facing endpoints require authentication
- Conversion service has sufficient CPU/memory to run 4 concurrent Docling processes (recommend 8GB RAM minimum)
- Bookmarks represent relatively static content; frequent re-scraping for content updates is out of scope
- arXiv HTML export is preferred when available; project accepts occasional arXiv API rate limits
- GitHub API rate limits allow fetching READMEs without authentication for reasonable bookmark volumes (default: 60 req/hour)
- SQLite WAL mode provides sufficient concurrency for 4 workers writing to single database file
- File system supports Unicode filenames and permits concurrent reads/writes to separate directories
- Temp workspace has sufficient disk space for largest expected artifacts (recommend 10GB minimum)
- S3 bucket has unlimited storage or sufficient quota; no object versioning required
- Manager component will implement batch submission and backoff logic; conversion service only needs to signal backpressure
- Observability infrastructure (metrics collector, log aggregation) is external; service only needs to emit structured logs and metrics
- Web UI users are trusted operators with localhost or VPN access; CSRF protection deferred to deployment configuration
- Markdown output is target format; no HTML preservation or alternative formats required
- Figure extraction preserves order and basic metadata (dimensions); advanced OCR or caption extraction out of scope
- Payload version bumps are manual operator actions or automated via CI/CD; no runtime auto-detection of upstream content changes
- Job retry attempts use exponential backoff with configurable max attempts (default: 3); no manual backoff override
- Database migrations are manual additive operations; no automated schema versioning or rollback required

## Dependencies

- Docling Python library (HTML and PDF conversion pipelines)
- FastAPI framework for REST API
- SQLModel or SQLAlchemy for SQLite ORM
- boto3 or aioboto3 for S3 client
- xxhash library for content hashing
- httpx or aiohttp for HTTP client with timeout/retry support
- Pydantic for configuration management and settings validation
- Python 3.10+ (for Pydantic v2 and async/await support)

## Out of Scope

- User authentication or authorization (internal-only service)
- Real-time job progress updates or WebSocket streaming
- Headless browser rendering for JavaScript-heavy HTML sources
- OCR for scanned PDFs or advanced figure caption extraction
- Automated content change detection or scheduled re-scraping
- Database migration framework or automated schema versioning
- Multi-region S3 replication or cross-region failover
- Advanced retry policies (e.g., per-domain rate limits, circuit breakers)
- Job prioritization based on user identity or external signals (only numeric priority field)
- Markdown post-processing (e.g., link rewriting, heading normalization)
- Asset cleanup or S3 lifecycle policies (manual or external automation)
- Prefect workflow implementation details (covered in separate planning phase)
- Web UI authentication, rate limiting, or CSRF protection (deployment configuration)
- Integration with KaraKeep API for bookmark ingestion (manager component responsibility)
