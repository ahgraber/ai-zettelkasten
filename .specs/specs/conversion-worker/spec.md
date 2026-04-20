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

The system SHALL store bookmark URLs in a normalized form such that URLs differing only by fragment, query-parameter ordering, or domain casing share an identical stored representation.

#### Scenario: Normalized URL stored on worker fetch

- **GIVEN** two bookmarks whose URLs differ only in domain casing, fragment, or query-parameter order
- **WHEN** each URL is recorded by the worker
- **THEN** both URLs are stored as the same string

### Requirement: Create conversion jobs with idempotency protection

The system SHALL assign each conversion job an idempotency key that is stable across resubmissions with identical contributing inputs and distinct whenever any contributing input differs, and SHALL reject submissions whose key matches an existing record.
Contributing inputs are: the internal bookmark identifier, the payload version, the Docling version, the Docling configuration fields that affect replayable output, and whether picture description is enabled.
A Docling configuration field contributes to the key if and only if its value affects replayable output; fields that only identify an external provider, authenticate to one, or control transport behavior without affecting output SHALL NOT contribute.

#### Scenario: New job created for unique parameters

- **GIVEN** no existing job has a matching idempotency key
- **WHEN** a conversion job is submitted
- **THEN** a new job record is created with status NEW and the computed idempotency key

#### Scenario: Duplicate job rejected

- **GIVEN** an existing job has the same idempotency key
- **WHEN** a duplicate submission is received
- **THEN** the submission is rejected and the existing job details are returned without creating a new record

#### Scenario: Key differs when picture classification enabled vs disabled

- **GIVEN** two conversion submissions for the same bookmark with identical other config
- **WHEN** one has `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED=true` and the other `false`
- **THEN** the two submissions produce different idempotency keys and are treated as distinct jobs

#### Scenario: Key stable when only the picture-description endpoint URL or API key rotates

- **GIVEN** two submissions with identical bookmark, payload version, Docling version, Docling output-affecting configuration, and picture-description enablement
- **WHEN** the two submissions differ only in the value of `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` or `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY` (with both still configured in each case)
- **THEN** the two submissions produce the same idempotency key and the second is rejected as a duplicate

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

The system SHALL convert each source document to Markdown and SHALL extract every embedded figure as an individually addressable image file with a sequentially determined name.
When picture classification is enabled and a picture-description endpoint is configured, each extracted figure SHALL receive a description whose form matches its figure type: chart-type figures receive chart summaries, table-type figures receive tabular-form descriptions, and all other figures receive generic alt-text.
When picture classification is disabled, each figure SHALL receive a single generic description.

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

#### Scenario: Chart figure described with chart2summary prompt

- **GIVEN** a PDF figure is classified as a chart type by DocumentFigureClassifier
- **WHEN** the post-conversion enrichment pass runs
- **THEN** the enrichment loop calls the VLM with a `<chart2summary>` prompt for that figure
- **AND** the resulting description is injected as a `PictureDescriptionData` annotation

#### Scenario: Table-image figure described with tables_html prompt

- **GIVEN** a PDF figure is classified as a table type by DocumentFigureClassifier
- **WHEN** the post-conversion enrichment pass runs
- **THEN** the enrichment loop calls the VLM with a `<tables_html>` prompt for that figure
- **AND** the resulting description is injected as a `PictureDescriptionData` annotation

#### Scenario: Unclassified or photo figure uses generic prompt

- **GIVEN** a PDF figure has no classification label, or is classified as photograph/logo/other
- **WHEN** the post-conversion enrichment pass runs
- **THEN** the enrichment loop calls the VLM with the existing generic alt-text prompt
- **AND** the resulting description is injected as a `PictureDescriptionData` annotation

#### Scenario: Picture classification disabled via config

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED=false`
- **WHEN** the PDF pipeline is configured
- **THEN** `do_picture_classification=False` and the enrichment pass falls back to the existing single-prompt Docling built-in description (no classification-based routing)

### Requirement: Serialize Markdown output with figure annotations

When serializing a `PictureItem` to Markdown, the system SHALL append annotation blocks as HTML comments following the image placeholder.
If a `PictureDescriptionData` annotation is present, the serializer emits a `<!-- Figure Description -->` comment block containing the description text.
If a `PictureClassificationData` annotation is also present, the serializer prepends a `<!-- Figure Type: <label> -->` comment immediately before the description block, enabling downstream consumers to filter or route by figure type.

#### Scenario: Classification label included in serialized output

- **GIVEN** a `PictureItem` has both a `PictureClassificationData` and a `PictureDescriptionData` annotation
- **WHEN** the item is serialized to Markdown
- **THEN** the output contains `<!-- Figure Type: <label> -->` followed by the description block

#### Scenario: No classification label when classifier disabled

- **GIVEN** `do_picture_classification=False` and no `PictureClassificationData` annotation exists
- **WHEN** the item is serialized to Markdown
- **THEN** the output contains only the description block, unchanged from prior behavior

### Requirement: Normalize whitespace in Markdown output

The system SHALL normalize whitespace in the Markdown output before writing to the output file and computing its content hash.
Normalization collapses multiple consecutive spaces to a single space and collapses 3 or more consecutive newlines to exactly 2 newlines.

#### Scenario: Multiple spaces collapsed on write

- **GIVEN** Docling conversion produces Markdown with multiple consecutive spaces
- **WHEN** the worker prepares to write `output.md`
- **THEN** each run of 2+ spaces is collapsed to a single space

#### Scenario: Multiple newlines collapsed on write

- **GIVEN** Docling conversion produces Markdown with 3 or more consecutive newlines
- **WHEN** the worker prepares to write `output.md`
- **THEN** each run of 3+ newlines is collapsed to exactly 2 newlines

#### Scenario: Indentation in code blocks preserved

- **GIVEN** the Markdown contains code blocks with intentional indentation
- **WHEN** whitespace normalization is applied
- **THEN** the indentation within code blocks is preserved and not collapsed

#### Scenario: Leading indentation for list nesting preserved

<!-- markdownlint-disable MD038 -->

- **GIVEN** the Markdown contains a nested list where nesting level is encoded by leading spaces (e.g. `- nested item` or `  - deeper item`)
- **WHEN** whitespace normalization is applied
- **THEN** the leading spaces on each line are preserved exactly, so list nesting structure is not altered

<!-- markdownlint-enable -->

#### Scenario: Trailing spaces on lines stripped

- **GIVEN** the Markdown contains lines with one or more trailing spaces before the newline
- **WHEN** whitespace normalization is applied
- **THEN** all trailing spaces before newlines are removed
- **AND** no two-space hard line breaks are introduced, because Docling never emits them

#### Scenario: Tab characters expanded to spaces

- **GIVEN** the Markdown contains tab characters outside code blocks
- **WHEN** whitespace normalization is applied
- **THEN** each tab is replaced by four spaces, which are then subject to space collapsing

#### Scenario: Hash computed on normalized Markdown

- **GIVEN** normalization modifies the Markdown text
- **WHEN** the content hash is computed
- **THEN** the hash is computed over the normalized Markdown, ensuring consistency across reruns with identical input

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

The system SHALL compare the new content hash against prior conversion outputs for the same bookmark and reuse the existing S3 location if the hashes match, skipping re-upload.

#### Scenario: Matching hash reuses existing artifacts

- **GIVEN** a reprocessed bookmark produces Markdown with the same content hash as a prior output for the same bookmark
- **WHEN** the worker completes conversion
- **THEN** the existing S3 artifacts are reused and a new output record is created pointing to the existing location without re-uploading

#### Scenario: Changed hash overwrites artifacts

- **GIVEN** a reprocessed bookmark produces Markdown with a different content hash
- **WHEN** the worker completes upload
- **THEN** S3 artifacts are uploaded and a new output record is created

#### Scenario: No prior output skips hash comparison

- **GIVEN** the bookmark has no prior succeeded conversion output
- **WHEN** the worker completes conversion
- **THEN** the worker proceeds with a full upload without attempting a hash comparison

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

The system SHALL process conversion jobs in first-in-first-out order by queue time, with the number of concurrently processing jobs bounded by a configurable limit.
Job claiming SHALL be atomic — the same job SHALL NOT be claimed and processed by two workers concurrently.

#### Scenario: Concurrent jobs processed up to limit

- **GIVEN** more jobs are queued than the concurrency limit
- **WHEN** the main thread polls for work
- **THEN** at most the configured number of jobs run simultaneously in worker threads and jobs are selected in queue-time order

#### Scenario: Main thread fills worker slots greedily

- **GIVEN** multiple jobs are queued and worker slots are available
- **WHEN** the main thread polls for work
- **THEN** it claims and dispatches jobs until all worker slots are filled or no more jobs are eligible

### Requirement: Bound concurrency of GPU-consuming conversion phases

The system SHALL bound the number of conversion phases running concurrently on the GPU by a configurable limit, to prevent GPU memory exhaustion.
Phases that do not use the GPU (preflight, upload) SHALL NOT count against this limit, so they proceed concurrently with GPU-bound conversion phases.

#### Scenario: GPU concurrency limit enforced

- **GIVEN** the GPU concurrency limit is 1 and one conversion subprocess is already running
- **WHEN** a second job reaches the conversion phase
- **THEN** the second job blocks until the first subprocess completes before spawning its own subprocess

#### Scenario: Non-GPU phases run concurrently

- **GIVEN** the GPU concurrency limit is 1 and one job is converting on GPU
- **WHEN** other jobs are in preflight or upload phases
- **THEN** those phases proceed without waiting for the GPU slot

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

The system SHALL load all configuration (S3 credentials, KaraKeep endpoint, concurrency limits, timeouts) from environment variables with sensible defaults for local development, and SHALL validate required service reachability before entering the main processing loop.
The picture description endpoint is configured via `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL`, `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY`, and `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_MODEL`.

#### Scenario: Configuration loaded on startup

- **GIVEN** environment variables are set
- **WHEN** the worker starts
- **THEN** all configuration is read from the environment without requiring code changes

### Requirement: Load picture classification configuration from environment

The system SHALL expose `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED` as a boolean environment variable (default: `True`) that controls whether the DocumentFigureClassifier runs during PDF conversion and whether the post-conversion enrichment pass performs classification-based prompt routing.

#### Scenario: Classification enabled by default

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED` is not set
- **WHEN** the worker starts
- **THEN** the PDF pipeline runs with `do_picture_classification=True`

#### Scenario: Classification disabled via environment

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED=false`
- **WHEN** the worker starts
- **THEN** the PDF pipeline runs with `do_picture_classification=False` and the enrichment pass does not attempt classification-based routing

### Requirement: Validate required external services on startup

The system SHALL probe required external services (S3 storage, KaraKeep API, and — when configured — the picture description endpoint) at process startup and SHALL refuse to start if any required service is unreachable.
When `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` and `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY` are both set, the probe issues `GET {base_url}/models` with an `Authorization: Bearer` header and a 10-second timeout; a non-2xx response or connection error prevents startup.
If neither field is set, the picture description probe is a no-op.
Probes SHALL use bounded timeouts to avoid hanging on unresponsive services.

#### Scenario: S3 reachable at startup

- **GIVEN** valid S3 credentials and endpoint are configured
- **WHEN** the worker or API process starts
- **THEN** a HEAD bucket probe succeeds within the timeout and the process continues startup

#### Scenario: S3 unreachable at startup

- **GIVEN** S3 credentials are invalid or the endpoint is unreachable
- **WHEN** the worker or API process starts
- **THEN** the process logs a structured error identifying the S3 failure and exits with a non-zero exit code

#### Scenario: KaraKeep API reachable at startup

- **GIVEN** a valid KaraKeep base URL and API key are configured
- **WHEN** the worker or API process starts
- **THEN** a health probe to the KaraKeep API succeeds within the timeout and the process continues startup

#### Scenario: KaraKeep API unreachable at startup

- **GIVEN** the KaraKeep API is unreachable or returns an error
- **WHEN** the worker or API process starts
- **THEN** the process logs a structured error identifying the KaraKeep failure and exits with a non-zero exit code

#### Scenario: Picture description endpoint reachable at startup

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` and `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY` are configured
- **WHEN** the worker or API process starts
- **THEN** `GET {base_url}/models` is called with an Authorization header and the process continues if it returns 2xx

#### Scenario: Picture description endpoint unreachable at startup

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` is configured but the endpoint is unreachable or returns non-2xx
- **WHEN** the worker or API process starts
- **THEN** the process logs a structured error identifying the failure and exits with a non-zero exit code

#### Scenario: Picture description not configured — probe skipped

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` is not set
- **WHEN** the worker or API process starts
- **THEN** no probe is made for the picture description endpoint and startup proceeds normally

### Requirement: Log optional feature status summary on startup

The system SHALL log a structured summary of all optional feature states on startup, indicating which features are enabled and which are disabled with the reason (missing configuration).
Optional features include picture descriptions, picture classification, MLflow tracing, and Litestream replication.
Picture classification reports as enabled only when both `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED=true` and `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` is configured; otherwise it reports as disabled with a specific reason.

#### Scenario: All optional features enabled

- **GIVEN** picture description, MLflow tracing, and Litestream replication are all configured
- **WHEN** the process starts
- **THEN** the startup summary log entry lists all three features as enabled

#### Scenario: Optional feature disabled due to missing config

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` or `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY` is not configured
- **WHEN** the process starts
- **THEN** the startup summary log entry lists picture descriptions as disabled with the reason `"AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL not configured"`

#### Scenario: Multiple features disabled

- **GIVEN** MLflow tracing and Litestream replication are both unconfigured
- **WHEN** the process starts
- **THEN** the startup summary log entry lists both features as disabled with their respective reasons

#### Scenario: Picture classification enabled in startup summary

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED=true` and `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` is configured
- **WHEN** the process starts
- **THEN** the startup summary log entry lists picture classification as enabled

#### Scenario: Picture classification disabled due to config flag

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED=false`
- **WHEN** the process starts
- **THEN** the startup summary log entry lists picture classification as disabled with reason "AIZK_CONVERTER\_\_DOCLING\_\_PICTURE_CLASSIFICATION_ENABLED=false"

#### Scenario: Picture classification implicitly disabled due to no VLM endpoint

- **GIVEN** `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` is not configured (picture description is disabled)
- **WHEN** the process starts
- **THEN** the startup summary log entry lists picture classification as disabled with reason "picture description not enabled"

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
configured `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` and `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY`) as an input to the idempotency key, so that jobs processed
with and without LLM figure descriptions produce distinct keys.

#### Scenario: Key differs when picture description enabled vs disabled

- **GIVEN** two conversion submissions for the same bookmark with identical Docling config and
  payload version
- **WHEN** one submission has `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` and `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY` configured and the other does not
- **THEN** the two submissions produce different idempotency keys and are treated as distinct jobs

#### Scenario: Key stable when picture description capability unchanged

- **GIVEN** a resubmission with the same bookmark, Docling config, payload version, and picture
  description capability flag
- **WHEN** the idempotency key is computed
- **THEN** the key matches the existing job and the submission is rejected as a duplicate

### Requirement: Persist conversion config in the manifest

The system SHALL write the Docling configuration fields that affect replayable output into the S3 manifest as a `config_snapshot` section, so the conversion can be replayed with identical parameters.
A Docling configuration field appears in `config_snapshot` if and only if its value affects replayable output; provider-identity fields, credentials, and transport-only controls SHALL NOT appear.
Independent of the replay criterion, the system SHALL NOT persist any credential, secret, or access token into the manifest; secrets MUST NOT be written to durable artifact storage under any circumstance.

#### Scenario: Manifest contains Docling config snapshot

- **GIVEN** a conversion completes successfully
- **WHEN** the manifest is written to the ephemeral workspace
- **THEN** the manifest includes the Docling configuration fields that affect replayable output (OCR settings, table structure, picture description model (`picture_description_model`), page limit, picture timeout, picture classification enabled) and the picture description enabled flag as a `config_snapshot` section

#### Scenario: Manifest captures picture classification flag

- **GIVEN** a conversion completes with `picture_classification_enabled=True`
- **WHEN** the manifest is written
- **THEN** the `config_snapshot` section includes `"picture_classification_enabled": true`

#### Scenario: Config snapshot matches idempotency key inputs

- **GIVEN** a manifest is present for a completed conversion
- **WHEN** the config snapshot is read from the manifest
- **THEN** the fields present are exactly those used to compute the idempotency key, enabling exact replay

#### Scenario: Manifest omits picture-description provider identity and credentials

- **GIVEN** a conversion completes successfully with `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL` and `AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY` both configured to non-empty values
- **WHEN** the `config_snapshot` section is read from the manifest
- **THEN** the section contains no entry for the picture-description endpoint URL and no entry for the picture-description API key, illustrating the general rule that provider identity and credentials are not persisted

## Technical Notes

- **Implementation**: `aizk/conversion/`
- **Dependencies**: conversion-api (shared data model); mlflow-llm-tracing (optional instrumentation); worker-process-management (subprocess lifecycle)
- **Data model**: Bookmark (karakeep_id unique key, aizk_uuid internal identifier), ConversionJob (status, idempotency_key unique, retry scheduling), ConversionOutput (artifact locations, content hash, pipeline metadata)
- **Idempotency key**: hash of `aizk_uuid + payload_version + docling_version + config_hash + picture_description_enabled`; `config_hash` covers the converter output-affecting fields (e.g., OCR, table structure, page limit, picture timeout, picture description model, `picture_classification_enabled`) and excludes provider identity and credentials (`picture_description_base_url`, `picture_description_api_key`)
- **Raw source provenance**: KaraKeep is the authoritative store for raw source content; `karakeep_id` is the durable provenance reference.
  Raw bytes are not archived locally.
- **Whitespace normalization**: `aizk/conversion/utilities/whitespace.py` → `normalize_whitespace()`; applied in `_run_conversion()` before file write and hash computation; preserves code-fence content, list indentation, and strips trailing spaces
- **Content hash**: xxhash64 of normalized Markdown (UTF-8, LF line endings) stored in the output record
- **Manifest config_snapshot**: manifest includes a `config_snapshot` section with the Docling config fields that affect replayable output (including `picture_classification_enabled`) and `picture_description_enabled` to enable exact replay; provider identity and credentials are excluded, and secrets are never persisted to durable artifact storage.
  The snapshot contract is enforced by `ManifestConfigSnapshot` (pydantic model with `extra="forbid"`) in `aizk/conversion/storage/manifest.py`.
- **S3 layout**: artifacts at `s3://<bucket>/<aizk_uuid>/`; upload verification via ETag match (single-part) or content length (multipart)
- **Database**: SQLite in WAL mode with foreign keys, synchronous=NORMAL, and busy timeout; indexes on job status/scheduling, bookmark URL, and output lookup by bookmark identifier
- **Workspace**: ephemeral temp directory cleaned up via context manager; figures named figure-001.png, figure-002.png, etc.
- **arXiv client**: `aizk.utilities.arxiv_utils`; URL parsing: `aizk.utilities.url_utils`
- **Process role labeling**: implemented via `setproctitle`
- **Startup validation**: `aizk/conversion/utilities/startup.py` → `validate_startup()`; probes S3 (HEAD bucket), KaraKeep (GET bookmarks?limit=1), and — when configured — picture description endpoint (GET /models) with 10s timeouts; logs feature summary for picture descriptions, picture classification, MLflow, and Litestream
- **Picture classification**: `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED` (default: `True`); controls `ThreadedPdfPipelineOptions.do_picture_classification` and the post-conversion enrichment loop in `converter.py`; prompt routing table `_LABEL_TO_PROMPT` maps classifier labels to `<chart2summary>` / `<tables_html>` / generic alt-text
