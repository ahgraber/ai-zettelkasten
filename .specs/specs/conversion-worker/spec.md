# Conversion Worker Specification

> Translated from Spec Kit on 2026-03-21
> Source: specs/001-docling-conversion-service/spec.md

## Purpose

The conversion worker processes queued bookmark conversion jobs end-to-end: fetching source content from KaraKeep, converting documents to Markdown using Docling, uploading artifacts to S3, and persisting results.
It enforces idempotency, manages concurrency, and emits structured logs and metrics for operational visibility.

## Requirements

### Requirement: Register bookmarks with stable internal identifiers

The system SHALL assign or look up a stable internal identifier for each bookmark keyed on the KaraKeep bookmark identifier, and SHALL persist the bookmark record before creating a conversion job.

#### Scenario: New bookmark registered on first submission

- **GIVEN** a submission references a KaraKeep bookmark identifier not previously seen
- **WHEN** the worker processes the job
- **THEN** a new bookmark record is created with a stable internal identifier and the KaraKeep identifier as a unique key

#### Scenario: Existing bookmark reused on resubmission

- **GIVEN** a submission references a KaraKeep bookmark identifier already in the system
- **WHEN** the worker processes the job
- **THEN** the existing bookmark record is reused and a new conversion job is created against it

### Requirement: Normalize URLs for deduplication

The system SHALL normalize bookmark URLs by removing fragments, sorting query parameters, and lowercasing the domain before storing them.

#### Scenario: Normalized URL stored on worker fetch

- **GIVEN** the worker fetches bookmark metadata from KaraKeep
- **WHEN** a URL is recorded for the bookmark
- **THEN** the stored URL has its domain lowercased, fragments removed, and query parameters sorted

### Requirement: Create conversion jobs with idempotency protection

The system SHALL create a conversion job record with a computed idempotency key and SHALL reject submissions whose key matches an existing record.
The idempotency key is a hash of `aizk_uuid + payload_version + docling_version + config_hash + picture_description_enabled`, where `picture_description_enabled` is a boolean derived from whether a chat completions endpoint is configured.

#### Scenario: New job created for unique parameters

- **GIVEN** no existing job has a matching idempotency key
- **WHEN** a conversion job is submitted
- **THEN** a new job record is created with status NEW and the computed idempotency key

#### Scenario: Duplicate job rejected

- **GIVEN** an existing job has the same idempotency key
- **WHEN** a duplicate submission is received
- **THEN** the submission is rejected and the existing job details are returned without creating a new record

### Requirement: Validate source content before conversion

The system SHALL fetch the KaraKeep bookmark and validate it has at least one of: HTML content, text, or PDF asset.
The system SHALL detect content type and source type from the bookmark structure and URL.

#### Scenario: Bookmark with missing content rejected

- **GIVEN** a KaraKeep bookmark has no HTML content, text, or PDF asset
- **WHEN** the worker processes the job
- **THEN** the job is marked permanently failed with a missing-content error code

#### Scenario: Content type detected from bookmark structure

- **GIVEN** a KaraKeep bookmark has a PDF asset
- **WHEN** the worker inspects the bookmark
- **THEN** the content type is recorded as PDF; if only HTML content or text is present, the content type is recorded as HTML

#### Scenario: Source type detected from URL

- **GIVEN** a KaraKeep bookmark URL is inspected
- **WHEN** the URL domain matches a known source pattern
- **THEN** the source type is recorded as arxiv for arxiv.org URLs, github for github.com URLs, and other otherwise

### Requirement: Fetch source content from KaraKeep

The system SHALL fetch source content from KaraKeep as the authoritative source using retry logic with exponential backoff and a configurable timeout.

#### Scenario: Successful content fetch

- **GIVEN** a KaraKeep bookmark has accessible content
- **WHEN** the worker fetches the bookmark
- **THEN** the content is retrieved within the configured timeout

#### Scenario: Fetch fails transiently

- **GIVEN** KaraKeep returns an error or times out
- **WHEN** the worker exhausts its retry attempts
- **THEN** the job is marked as retryable-failed with the HTTP status or timeout recorded

#### Scenario: PDF asset fetched from KaraKeep

- **GIVEN** the bookmark has a PDF asset type
- **WHEN** the worker processes the job
- **THEN** the PDF asset bytes are fetched from KaraKeep before conversion

### Requirement: Process arXiv bookmarks by downloading PDFs

The system SHALL download arXiv papers as PDFs for conversion, using the abstract page URL to resolve the paper identifier when no PDF asset is present.

#### Scenario: Abstract page bookmark downloads PDF

- **GIVEN** an arXiv bookmark with an abstract page URL (arxiv.org/abs/...)
- **WHEN** the worker processes the job
- **THEN** the system resolves the arXiv paper identifier and downloads the PDF for conversion

#### Scenario: arXiv PDF asset fetched from KaraKeep

- **GIVEN** an arXiv bookmark has a PDF asset stored in KaraKeep
- **WHEN** the worker processes the job
- **THEN** the PDF is fetched from KaraKeep and converted to Markdown

#### Scenario: arXiv link bookmark with HTML uses arxiv_pdf_url

- **GIVEN** an arXiv bookmark has HTML content and an arxiv_pdf_url metadata field
- **WHEN** the worker processes the job
- **THEN** the system downloads the PDF from the arxiv_pdf_url and converts it to Markdown

### Requirement: Process GitHub bookmarks by fetching README content

The system SHALL extract the repository owner and name from a GitHub URL and fetch the README from the default branch, preferring Markdown over reStructuredText over plain text.

#### Scenario: GitHub README fetched and converted

- **GIVEN** a GitHub bookmark with a valid repository URL
- **WHEN** the worker processes the job
- **THEN** the README content is fetched from the default branch and converted to Markdown

#### Scenario: GitHub repository with no README fails permanently

- **GIVEN** a GitHub bookmark's repository has no README file
- **WHEN** the worker attempts to fetch the README
- **THEN** the job is marked permanently failed

### Requirement: Convert documents to Markdown and extract figures

The system SHALL run the appropriate Docling conversion pipeline for the source content type and extract figures as individual image files with sequential naming.

#### Scenario: HTML document converted to Markdown

- **GIVEN** a bookmark with HTML content type
- **WHEN** the worker runs conversion
- **THEN** Markdown output and any extracted figures are produced using the HTML pipeline

#### Scenario: PDF document converted to Markdown with figures

- **GIVEN** a bookmark with PDF content type
- **WHEN** the worker runs conversion
- **THEN** Markdown output is produced and figures are extracted as sequentially named image files

#### Scenario: Empty Markdown output fails permanently

- **GIVEN** Docling conversion produces empty or invalid Markdown
- **WHEN** the worker inspects the output
- **THEN** the job is marked permanently failed and any existing successful output is preserved

### Requirement: Compute a content hash for deduplication

The system SHALL compute a content hash of the normalized Markdown output and store it with the conversion output record.

#### Scenario: Hash computed and stored

- **GIVEN** conversion produces valid Markdown output
- **WHEN** the worker stores the conversion output
- **THEN** a content hash of the normalized Markdown (UTF-8, LF line endings) is computed and persisted

### Requirement: Write conversion artifacts to an ephemeral workspace

The system SHALL write Markdown, figures, and a manifest to an ephemeral temporary workspace during processing, and SHALL clean up the workspace automatically when the job finishes.

#### Scenario: Workspace cleaned up on success

- **GIVEN** a job completes successfully
- **WHEN** the worker finishes uploading artifacts
- **THEN** the temporary workspace directory is removed

#### Scenario: Workspace cleaned up on failure

- **GIVEN** a job fails during any processing phase
- **WHEN** the error handler runs
- **THEN** the temporary workspace is cleaned up

### Requirement: Upload artifacts to S3 and verify before marking success

The system SHALL upload all conversion artifacts to S3 and verify each upload before transitioning the job to succeeded.

#### Scenario: Successful upload and verification

- **GIVEN** conversion artifacts are ready in the workspace
- **WHEN** the worker uploads all artifacts and verification passes
- **THEN** the job status transitions to SUCCEEDED only after all uploads are confirmed

#### Scenario: S3 upload fails transiently

- **GIVEN** an S3 upload fails due to a transient error
- **WHEN** the worker cannot complete all uploads
- **THEN** the job status transitions to UPLOAD_PENDING for retry without re-running conversion; if cached artifacts are missing, the job falls back to retryable-failed for a full retry

#### Scenario: Upload failure does not mark job succeeded

- **GIVEN** an S3 upload fails or verification fails
- **WHEN** the error is detected
- **THEN** the job is not marked SUCCEEDED and no partial artifact paths are published

### Requirement: Skip S3 overwrite when content hash matches

The system SHALL compare the new content hash against the most recent conversion output and reuse the existing S3 location if the hashes match.

#### Scenario: Matching hash reuses existing artifacts

- **GIVEN** a reprocessed bookmark produces Markdown with the same content hash as the previous output
- **WHEN** the worker completes conversion
- **THEN** the existing S3 artifacts are reused and a new output record is created pointing to the existing location without overwriting

#### Scenario: Changed hash overwrites artifacts

- **GIVEN** a reprocessed bookmark produces Markdown with a different content hash
- **WHEN** the worker completes upload
- **THEN** S3 artifacts are overwritten and a new output record is created

### Requirement: Create a conversion output record on success

The system SHALL create a conversion output record capturing artifact locations, content hash, figure count, pipeline metadata, Docling version, and the config snapshot used for the conversion on successful job completion.

#### Scenario: Output record created after successful upload

- **GIVEN** all artifacts are uploaded and verified
- **WHEN** the worker finalizes the job
- **THEN** a conversion output record is created with S3 prefixes, Markdown key, manifest key, content hash, figure count, Docling version, pipeline name, and timestamps

### Requirement: Transition job status atomically

The system SHALL update job status to SUCCEEDED, FAILED_RETRYABLE, or FAILED_PERM in a database transaction only after the associated S3 or error state is confirmed.

#### Scenario: Status set to SUCCEEDED after verified upload

- **GIVEN** all S3 uploads are verified
- **WHEN** the transaction commits
- **THEN** the job status is SUCCEEDED and the output record is visible to consumers

#### Scenario: Retryable error sets status to FAILED_RETRYABLE

- **GIVEN** a transient error occurs during fetch, conversion, or upload
- **WHEN** the error handler runs
- **THEN** the job status transitions to FAILED_RETRYABLE with the error code and message recorded

#### Scenario: Permanent error sets status to FAILED_PERM

- **GIVEN** a non-recoverable error occurs (missing content, empty output)
- **WHEN** the error handler runs
- **THEN** the job status transitions to FAILED_PERM with the error code and message recorded

### Requirement: Process jobs with bounded concurrency in FIFO order

The system SHALL process conversion jobs with a configurable number of parallel workers (default: 4) in first-in-first-out order by queue time.

#### Scenario: Concurrent jobs processed up to limit

- **GIVEN** more jobs are queued than the concurrency limit
- **WHEN** workers poll for work
- **THEN** at most the configured number of jobs run simultaneously and jobs are selected in queue-time order

### Requirement: Store Markdown output as a standardized filename

The system SHALL store the Markdown artifact as a file named `output.md` regardless of the bookmark title.

#### Scenario: Markdown artifact named consistently

- **GIVEN** conversion produces valid Markdown output
- **WHEN** the artifact is written to the workspace
- **THEN** the file is named `output.md`

### Requirement: Emit structured logs with trace context

The system SHALL log key processing events with job identifier, bookmark identifier, KaraKeep identifier, and status in every log entry to enable trace reconstruction.

#### Scenario: Log entries include trace context

- **GIVEN** a worker is processing a job
- **WHEN** any key event is logged
- **THEN** the log entry includes the job identifier, internal bookmark identifier, and KaraKeep identifier

### Requirement: Emit operational metrics

The system SHALL emit metrics for queue depth, job duration, job status counts, fetch latency, and S3 upload latency.

#### Scenario: Metrics emitted during processing

- **GIVEN** jobs are being processed
- **WHEN** a job transitions through lifecycle phases
- **THEN** the worker emits timing and status metrics for each measurable operation

### Requirement: Load configuration from environment variables

The system SHALL load all configuration (S3 credentials, KaraKeep endpoint, concurrency limits, timeouts) from environment variables with sensible defaults for local development.

#### Scenario: Configuration loaded on startup

- **GIVEN** environment variables are set
- **WHEN** the worker starts
- **THEN** all configuration is read from the environment without requiring code changes

### Requirement: Identify process role for operator monitoring

Every Python process (API server, worker, CLI) SHALL expose its role as a human-readable label to enable operators to distinguish process types during monitoring.

#### Scenario: Worker process identifiable by role

- **GIVEN** multiple process types are running on the same host
- **WHEN** an operator inspects the process list
- **THEN** each process is labeled with its role (API, worker, or CLI)

### Requirement: Declare KaraKeep as the authoritative raw input store

The system SHALL treat KaraKeep as the authoritative store for raw source content (HTML, text, and PDF assets) and SHALL record the KaraKeep bookmark identifier as the durable provenance reference for every conversion artifact.
Local copies of raw bytes are not required provided KaraKeep access is stable and the identifier is persisted.

#### Scenario: Provenance reference recorded for every bookmark

- **GIVEN** a bookmark is registered for conversion
- **WHEN** the bookmark record is created or looked up
- **THEN** the KaraKeep bookmark identifier is persisted as the stable provenance reference linking
  every derived artifact back to its authoritative source

#### Scenario: Raw bytes not stored locally

- **GIVEN** source content is fetched from KaraKeep for conversion
- **WHEN** the conversion completes and artifacts are uploaded
- **THEN** the raw HTML, text, or PDF bytes are not persisted beyond the ephemeral workspace; the
  KaraKeep identifier is sufficient as the durable raw-input reference

### Requirement: Include picture description capability in the idempotency key

The system SHALL include whether picture description is enabled (derived from the presence of a
configured chat completions endpoint) as an input to the idempotency key, so that jobs processed
with and without LLM figure descriptions produce distinct keys.

#### Scenario: Key differs when picture description enabled vs disabled

- **GIVEN** two conversion submissions for the same bookmark with identical Docling config and
  payload version
- **WHEN** one submission has a chat completions endpoint configured and the other does not
- **THEN** the two submissions produce different idempotency keys and are treated as distinct jobs

#### Scenario: Key stable when picture description capability unchanged

- **GIVEN** a resubmission with the same bookmark, Docling config, payload version, and picture
  description capability flag
- **WHEN** the idempotency key is computed
- **THEN** the key matches the existing job and the submission is rejected as a duplicate

### Requirement: Persist conversion config in the manifest

The system SHALL write the full Docling configuration snapshot used for a conversion into the S3
manifest, so the conversion can be replayed with identical parameters.

#### Scenario: Manifest contains Docling config snapshot

- **GIVEN** a conversion completes successfully
- **WHEN** the manifest is written to the ephemeral workspace
- **THEN** the manifest includes all Docling configuration fields (OCR settings, table structure,
  VLM model, page limit, picture timeout) and the picture description enabled flag as a config
  snapshot section

#### Scenario: Config snapshot matches idempotency key inputs

- **GIVEN** a manifest is present for a completed conversion
- **WHEN** the config snapshot is read from the manifest
- **THEN** the fields present are exactly those used to compute the idempotency key, enabling
  exact replay

## Technical Notes

- **Implementation**: `aizk/conversion/`
- **Dependencies**: conversion-api (shared data model); mlflow-llm-tracing (optional instrumentation); worker-process-management (subprocess lifecycle)
- **Data model**: Bookmark (karakeep_id unique key, aizk_uuid internal identifier), ConversionJob (status, idempotency_key unique, retry scheduling), ConversionOutput (artifact locations, content hash, pipeline metadata)
- **Idempotency key**: hash of `aizk_uuid + payload_version + docling_version + config_hash + picture_description_enabled`
- **Raw source provenance**: KaraKeep is the authoritative store for raw source content; `karakeep_id` is the durable provenance reference.
  Raw bytes are not archived locally.
- **Content hash**: xxhash64 of normalized Markdown (UTF-8, LF line endings) stored in the output record
- **Manifest config_snapshot**: manifest includes a `config_snapshot` section with all Docling config fields and `picture_description_enabled` to enable exact replay.
- **S3 layout**: artifacts at `s3://<bucket>/<aizk_uuid>/`; upload verification via ETag match (single-part) or content length (multipart)
- **Database**: SQLite in WAL mode with foreign keys, synchronous=NORMAL, and busy timeout; indexes on job status/scheduling, bookmark URL, and output lookup by bookmark identifier
- **Workspace**: ephemeral temp directory cleaned up via context manager; figures named figure-001.png, figure-002.png, etc.
- **arXiv client**: `aizk.utilities.arxiv_utils`; URL parsing: `aizk.utilities.url_utils`
- **Process role labeling**: implemented via `setproctitle`
