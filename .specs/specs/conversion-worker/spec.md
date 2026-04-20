# Conversion Worker Specification

> Translated from Spec Kit on 2026-03-21
> Source: specs/001-docling-conversion-service/spec.md

## Purpose

The conversion worker processes queued bookmark conversion jobs end-to-end: fetching source content from KaraKeep, converting documents to Markdown using Docling, uploading artifacts to S3, and persisting results.
It enforces idempotency, manages concurrency, and emits structured logs and metrics for operational visibility.

## Requirements

### Requirement: Enrich Source metadata from fetcher chain results

The system SHALL update the existing Source row's mutable metadata — `url`, `normalized_url`, `title`, `source_type`, `content_type` — from the resolver and fetcher chain results.
`source_type` SHALL be derived from `terminal_ref.kind` via a canonical `SOURCE_TYPE_BY_KIND` mapping defined in `aizk.conversion.core.types` (not emitted per-fetcher), so that the same kind produces a consistent `source_type` regardless of which adapter runs.
The worker SHALL NOT create Source rows, assign `aizk_uuid`, compute `source_ref_hash`, or modify the immutable identity columns (`aizk_uuid`, `source_ref`, `source_ref_hash`, `karakeep_id`); those are materialized by the API at submit time.
Enrichment SHALL follow last-writer-wins semantics across concurrent jobs for the same Source: the mutable columns are an advisory cache for UI/search, and each job's authoritative values are preserved in its own manifest.
Enrichment writes SHALL be best-effort: a failure writing to the Source row (e.g., a transient database error) SHALL be logged with the `aizk_uuid`, the failing column set, and the underlying error, but SHALL NOT fail the job — conversion proceeds and the manifest's authoritative values remain correct.
Each enrichment write SHALL be scoped to a single UPDATE statement per Source row (not wrapped in the job transaction), so a partial failure leaves the job record unaffected.
Replay after a retry is naturally idempotent under last-writer-wins; no retry-specific logic is required. (Previously: the orchestrator derived these fields from KaraKeep bookmark structure for every job.
Now the API owns identity, and the worker owns enrichment of mutable metadata.)

#### Scenario: Metadata enriched after fetch

- **GIVEN** a job references a Source row whose mutable metadata is empty and whose `source_ref` is a `UrlRef`
- **WHEN** the fetcher chain completes and returns a `ConversionInput` with authoritative content type
- **THEN** the Source row's `url`, `normalized_url`, `title`, `source_type`, and `content_type` are populated from the fetcher/resolver results

#### Scenario: Immutable columns never rewritten

- **GIVEN** a Source row with existing `aizk_uuid`, `source_ref`, `source_ref_hash`, and `karakeep_id`
- **WHEN** the worker enriches the row after a fetch
- **THEN** those four columns are unchanged regardless of fetcher output

#### Scenario: Enrichment failure does not fail the job

- **GIVEN** a fetcher chain completes successfully but the Source-row UPDATE fails (e.g., transient database error)
- **WHEN** the worker attempts to persist the enriched metadata
- **THEN** the failure is logged with `aizk_uuid`, the column set attempted, and the underlying error, and the conversion job proceeds to conversion and produces a valid manifest with the authoritative values

#### Scenario: source_type derived from terminal ref kind

- **GIVEN** a fetcher chain that terminates at an `ArxivRef` (whether submitted directly or resolved from a KaraKeep bookmark)
- **WHEN** the worker enriches the Source row
- **THEN** the Source row's `source_type` column is set to the value mapped from `terminal_ref.kind` by the canonical `SOURCE_TYPE_BY_KIND` mapping defined in `aizk.conversion.core.types` (e.g., `arxiv` kind → `"arxiv"`; `github_readme` kind → `"github"`; `url`, `karakeep_bookmark`, `inline_html`, `singlefile` → `"other"`)

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

### Requirement: Store source reference on the job record

The system SHALL store a `source_ref` JSON value on each conversion job record, carrying the typed source reference that the fetcher chain will resolve.
The `source_ref` SHALL be a valid member of the `SourceRef` discriminated union and SHALL round-trip through JSON without loss.
The `source_ref` is denormalized from the Source row onto the job record for fetch-chain use.

#### Scenario: Job created with source ref

- **GIVEN** a job is submitted with a `KarakeepBookmarkRef`
- **WHEN** the job record is created
- **THEN** the `source_ref` column contains the serialized ref with `kind: "karakeep_bookmark"`

#### Scenario: Source ref survives persistence round-trip

- **GIVEN** a job record with a stored `source_ref`
- **WHEN** the record is read back
- **THEN** the deserialized `SourceRef` matches the original variant and fields

### Requirement: Validate source content before conversion

The system SHALL validate that the fetched content is non-empty and carries a `ContentType` for which a converter is registered, before proceeding to conversion.
Content-type and source-type detection are the responsibility of the fetcher/resolver chain, not the orchestrator. (Previously: detection was performed by the orchestrator from KaraKeep bookmark structure and URL.)

#### Scenario: Fetcher returns empty content

- **GIVEN** a fetcher returns a `ConversionInput` with zero-length bytes
- **WHEN** the orchestrator inspects the input
- **THEN** the job is marked permanently failed with a missing-content error code

#### Scenario: No converter registered for content type

- **GIVEN** a fetcher returns a `ConversionInput` with a content type for which no converter is registered
- **WHEN** the orchestrator attempts to resolve a converter
- **THEN** the job is marked permanently failed with a `NoConverterForFormat` error

### Requirement: Fetch source content via the registered fetcher

The system SHALL fetch source content by dispatching on the job's `source_ref` through the fetcher registry, following any ref-resolver chain to a terminal content fetcher.
(Previously: the system fetched source content from KaraKeep as the single authoritative source.)

#### Scenario: Content fetched through resolver chain

- **GIVEN** a job with a `KarakeepBookmarkRef` source ref whose registered resolver returns an `ArxivRef`
- **WHEN** the worker fetches content
- **THEN** the arxiv content fetcher is invoked, producing a `ConversionInput`

#### Scenario: Content fetched directly

- **GIVEN** a job with a `UrlRef` source ref mapped to a content fetcher
- **WHEN** the worker fetches content
- **THEN** the URL content fetcher is invoked directly without resolver delegation

### Requirement: Process arXiv bookmarks by downloading PDFs

(Unchanged in behavior.
Relocated: this behavior is now provided by the `ArxivFetcher` content-fetcher adapter, invoked when the resolver chain produces an `ArxivRef`.)

The `KarakeepBookmarkResolver` SHALL preserve the existing resolution precedence when determining that a bookmark is arXiv-sourced, and the `ArxivFetcher` SHALL preserve the existing PDF-source precedence.

#### Scenario: ArXiv resolution precedence preserved

- **GIVEN** a `KarakeepBookmarkRef` for a bookmark whose `source_type == "arxiv"`
- **WHEN** the `KarakeepBookmarkResolver` resolves the ref
- **THEN** it returns an `ArxivRef` (not a `UrlRef` or `InlineHtmlRef`), preserving the arXiv-specific PDF pipeline

#### Scenario: ArXiv PDF source precedence — KaraKeep asset preferred

- **GIVEN** an `ArxivRef` for a bookmark that has both a KaraKeep PDF asset and an `arxiv_pdf_url` metadata field
- **WHEN** the `ArxivFetcher` fetches the PDF
- **THEN** the KaraKeep asset is used (avoids arxiv.org rate limits)

#### Scenario: ArXiv PDF source precedence — arxiv_pdf_url fallback

- **GIVEN** an `ArxivRef` for a bookmark with no KaraKeep PDF asset but an `arxiv_pdf_url` metadata field
- **WHEN** the `ArxivFetcher` fetches the PDF
- **THEN** the PDF is downloaded from the `arxiv_pdf_url`

#### Scenario: ArXiv PDF source precedence — abstract page resolution

- **GIVEN** an `ArxivRef` for a bookmark with only an abstract page URL (no asset, no `arxiv_pdf_url`)
- **WHEN** the `ArxivFetcher` fetches the PDF
- **THEN** the arXiv ID is resolved from the abstract URL and the PDF URL is constructed

### Requirement: Process GitHub bookmarks by fetching README content

(Unchanged in behavior.
Relocated: this behavior is now provided by the `GithubReadmeFetcher` content-fetcher adapter, invoked when the resolver chain produces a `GithubReadmeRef`.)

### Requirement: Convert documents to Markdown and extract figures

The system SHALL convert each source document to Markdown and extract figures by invoking the converter resolved for the job's content type and the deployment's configured converter name.
Converter-specific behavior (picture classification, figure enrichment, serialization annotations) is an internal concern of the converter adapter. (Previously: Docling was invoked directly by name with inline format dispatch.)

#### Scenario: Converter resolved and invoked

- **GIVEN** a `ConversionInput` with content type `pdf` and a configured converter name `"docling"`
- **WHEN** the orchestrator invokes conversion
- **THEN** the `DoclingConverter` is resolved from the registry and produces `ConversionArtifacts`

#### Scenario: Converter-specific enrichment remains internal

- **GIVEN** the `DoclingConverter` is invoked for a PDF with picture classification enabled
- **WHEN** conversion completes
- **THEN** figure descriptions and classification annotations are present in the output, as before, without the orchestrator having knowledge of these adapter-specific behaviors

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
- **THEN** a conversion output record is created with S3 prefixes, bare S3 Markdown key (e.g. `{uuid}/output.md`, no `s3://` URI prefix), bare S3 manifest key (e.g. `{uuid}/manifest.json`), content hash, figure count, Docling version, pipeline name, and timestamps

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

The system SHALL bound the number of GPU-consuming conversion subprocesses running concurrently via a GPU `ResourceGuard` context manager acquired in the parent process before subprocess spawn and held for the subprocess's full lifetime (spawn, supervision, and reap).
The guard SHALL be a threading primitive shared across the parent's worker thread pool.
The orchestrator SHALL acquire the guard if and only if the dispatched converter declares `requires_gpu == True`; a converter declaring `requires_gpu == False` SHALL spawn its subprocess without contending on the GPU guard.
Converter adapters running inside forked child processes SHALL NOT own or acquire the cross-job GPU guard.
The acquiring worker thread SHALL be the sole releaser; the supervision loop SHALL NOT call release directly, and the guard SHALL be released by normal or exceptional unwind of the acquiring thread's `with` block after subprocess reap.
Today the only registered converter is `DoclingConverter` with `requires_gpu = True`, so every conversion subprocess acquires the guard in the current deployment; the bypass path is a defined protocol capability for future non-GPU converters. (Previously: bounded by a module-level `threading.Semaphore` in the orchestrator.
Now the guard is wrapped as an injected `ResourceGuard` at the parent/supervision level, not inside converter adapters — because forked children would get independent semaphore copies, destroying the global cap.)

#### Scenario: GPU-consuming converter acquires guard

- **GIVEN** a job dispatched to a converter whose `requires_gpu == True` (e.g., `DoclingConverter`)
- **WHEN** the worker prepares to spawn the conversion subprocess
- **THEN** the worker thread enters the GPU guard's `with` block before spawning the subprocess

#### Scenario: Non-GPU converter bypasses guard

- **GIVEN** a (hypothetical) converter whose `requires_gpu == False`
- **WHEN** a job is dispatched to it
- **THEN** the subprocess is spawned without acquiring the GPU guard; concurrent GPU-bound jobs on other threads are not blocked by it

#### Scenario: Parent-side guard limits concurrent GPU subprocesses

- **GIVEN** the GPU concurrency limit is 1 and one worker thread has entered the guard's `with` block and spawned a GPU-consuming conversion subprocess
- **WHEN** a second worker thread attempts to enter the guard for another GPU-consuming job
- **THEN** the second thread blocks until the first thread's `with` block exits (after its subprocess is reaped)

#### Scenario: Guard held through subprocess reap

- **GIVEN** a worker thread holds the GPU guard and its subprocess has finished writing artifacts
- **WHEN** the supervision loop observes the subprocess exit
- **THEN** the guard remains held until the acquiring thread's `with` block exits after reap, not at the moment of exit detection

#### Scenario: Guard released on subprocess crash via acquiring thread

- **GIVEN** a worker thread holds the GPU guard and its conversion subprocess crashes
- **WHEN** the supervision loop surfaces the failure to the acquiring thread
- **THEN** the acquiring thread's `with` block unwinds, releasing the guard, and other threads may proceed

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

The system SHALL load configuration from environment variables organized into per-adapter nested namespaces.
Converter-specific settings live under the converter's namespace (e.g., `AIZK_CONVERTER__DOCLING__*`); fetcher-specific settings live under the fetcher's namespace.
Old flat-namespace env vars (`AIZK_DOCLING_*`) are removed without a compatibility shim. (Previously: flat `AIZK_DOCLING_*` namespace.)

#### Scenario: Nested env var read by adapter

- **GIVEN** `AIZK_CONVERTER__DOCLING__OCR_ENABLED=true` is set
- **WHEN** the DoclingConverter's config is loaded
- **THEN** the `ocr_enabled` field is `True`

#### Scenario: Old flat-namespace env var ignored

- **GIVEN** `AIZK_DOCLING_OCR_ENABLED=true` is set but no nested equivalent is present
- **WHEN** configuration is loaded
- **THEN** the value is not read; the field falls back to its library default

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

The system SHALL probe required external services at process startup, with the set of probes determined by the registered adapters and their configuration. (Previously: probes were hard-coded for S3, KaraKeep, and the picture-description endpoint.
Now each adapter may declare startup probes, and the composition root aggregates them.)

#### Scenario: Adapter-declared probe executed at startup

- **GIVEN** the `DoclingConverter` adapter declares a probe for the picture-description endpoint
- **WHEN** the worker starts
- **THEN** the probe is executed alongside S3 and database probes

#### Scenario: Unused adapter probe skipped

- **GIVEN** no KaraKeep fetcher is registered (e.g., in a deployment using only URL-based ingestion)
- **WHEN** the worker starts
- **THEN** no KaraKeep API probe is attempted

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

### Requirement: Declare provenance per source type

For KaraKeep-sourced jobs, the system SHALL treat KaraKeep as the authoritative store for raw source content and record the KaraKeep bookmark identifier as the durable provenance reference.
For non-KaraKeep-sourced jobs, the `source_ref` on the Source and job records SHALL serve as the durable provenance reference. (Previously: KaraKeep was unconditionally declared as the authoritative raw-input store.)

#### Scenario: KaraKeep provenance preserved

- **GIVEN** a job sourced from a KaraKeep bookmark
- **WHEN** the job completes
- **THEN** the KaraKeep bookmark identifier (on the Source row) is recorded as provenance, as before

#### Scenario: Non-KaraKeep provenance via source ref

- **GIVEN** a job sourced from a `UrlRef`
- **WHEN** the job completes
- **THEN** the `source_ref` (containing the URL) on the Source row serves as the provenance reference

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

### Requirement: Persist conversion config and source provenance in the manifest

The system SHALL write manifests in format version `"2.0"`.
Version 2.0 manifests SHALL:

- Carry a `config_snapshot` section including `converter_name` and the converter-adapter-supplied output-affecting fields (the orchestrator treats adapter fields as opaque).
  The config-snapshot model SHALL set `extra="forbid"` so unknown fields fail loudly at read time.
- Allow `ManifestSource.url`, `normalized_url`, `title`, `source_type`, and `fetched_at` to be `null` (required in v1.0) so non-KaraKeep jobs can serialize.
- Carry two typed ref blocks, both required:
  - `submitted_ref` — the `SourceRef` the caller supplied at submit time (ingress shape).
  - `terminal_ref` — the ref whose `ContentFetcher` actually produced the converted bytes (terminal fetch state), keyed on the terminal ref's kind (e.g., `bookmark_id` for `karakeep_bookmark`, `arxiv_id` for `arxiv`, `owner`/`repo` for `github_readme`, `url` for `url`, `content_hash` for `inline_html`).
- Both blocks are always present; for direct submissions (no resolver hop) they carry equal values.
- Move `karakeep_id` out of the top-level source block; it appears in `submitted_ref` (when the caller supplied a `KarakeepBookmarkRef`) and/or `terminal_ref` (when KaraKeep was the byte source).

Readers SHALL be implemented as version-specific classes (`ManifestV1`, `ManifestV2`), both with `extra="forbid"`; a version-dispatching loader SHALL select the reader class from the serialized `version` string. (Previously: manifest version `"1.0"` with a Docling-specific config section and required KaraKeep source fields.)

#### Scenario: KaraKeep-terminal job — submitted_ref equals terminal_ref

- **GIVEN** a conversion completes for a KaraKeep bookmark whose resolver chain terminates at the KaraKeep content fetcher (non-arxiv, non-github)
- **WHEN** the manifest is written
- **THEN** `version == "2.0"`, `submitted_ref.kind == "karakeep_bookmark"` and `terminal_ref.kind == "karakeep_bookmark"` with the `bookmark_id` populated in both, and the two blocks carry structurally equal values

#### Scenario: KaraKeep-to-arxiv job — submitted_ref and terminal_ref diverge

- **GIVEN** a conversion completes for a KaraKeep bookmark whose resolver returned an `ArxivRef` and the arxiv content fetcher ran
- **WHEN** the manifest is written
- **THEN** `submitted_ref.kind == "karakeep_bookmark"` with the original `bookmark_id`, and `terminal_ref.kind == "arxiv"` with the `arxiv_id` populated

#### Scenario: Direct UrlRef submission — both blocks equal

- **GIVEN** a conversion completes for a `UrlRef`-sourced job where the fetcher populated `url` but no `title`
- **WHEN** the manifest is written
- **THEN** `ManifestSource.title` is `null`, the manifest is valid, and both `submitted_ref.kind == "url"` and `terminal_ref.kind == "url"` carry equal values

#### Scenario: Reader dispatches by version

- **GIVEN** a previously written v1.0 manifest on S3
- **WHEN** the version-dispatching loader reads it
- **THEN** it selects the `ManifestV1` reader class, deserialization succeeds, and the `karakeep_id` field is read from the top-level source block

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
