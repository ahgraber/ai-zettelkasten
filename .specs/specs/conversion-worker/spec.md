# Conversion Worker Specification

> Translated from Spec Kit on 2026-03-21
> Source: specs/001-docling-conversion-service/spec.md

## Purpose

The conversion worker processes queued bookmark conversion jobs end-to-end: fetching source content from KaraKeep, converting documents to Markdown using Docling, uploading artifacts to S3, and persisting results.
It enforces idempotency, manages concurrency, and emits structured logs and metrics for operational visibility.

## Requirements

### Requirement: Select source_title in the subprocess from raw observations

The system SHALL select the canonical `source_title` for a job inside the conversion subprocess, after conversion completes, by combining `ConversionArtifacts.document_title` (raw converter output) with `source_meta.resolver_title` (resolver observation):

- The system SHALL use `document_title` as `source_title` when it is non-empty, does not match a UUID-shaped string (`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` or a hex-and-dash string of length 32–36), and does not begin with `http://` or `https://`.
- Otherwise the system SHALL use `resolver_title` when it is non-empty.
- Otherwise `source_title` SHALL be `None`.

The selected `source_title` SHALL be carried to the parent process as `SubprocessMetadata.source_title` and SHALL be the authoritative input for the manifest source block, the `ConversionOutput` row, and Source-row enrichment.
For the manifest and `Source.title`, `source_title = None` SHALL serialise as `null` / `NULL` directly (both are nullable).
For `ConversionOutput.title`, which is currently a non-null column, the worker SHALL fall back to the parent `ConversionJob.title` placeholder when `source_title is None`, so that `ConversionOutput.title = subprocess_meta.source_title or job.title`; this preserves the existing non-null output contract without forcing a schema migration.
The selection rule SHALL NOT live in the converter (which only reports raw `document_title`) nor in the parent process (which receives the already-selected value).

#### Scenario: Document title preferred when suitable

- **GIVEN** a job whose converter produced `document_title = "Attention Is All You Need"` and whose resolver supplied `resolver_title = "Untitled bookmark"`
- **WHEN** the subprocess selects `source_title`
- **THEN** `SubprocessMetadata.source_title == "Attention Is All You Need"`

#### Scenario: Resolver title used when document title is unsuitable

- **GIVEN** a job whose `document_title` is empty, UUID-shaped, or starts with `http`, and whose `resolver_title = "Project README"`
- **WHEN** the subprocess selects `source_title`
- **THEN** `SubprocessMetadata.source_title == "Project README"`

#### Scenario: Source title is None when neither input is suitable

- **GIVEN** a job whose `document_title` is `None` and whose `resolver_title` is `None`
- **WHEN** the subprocess selects `source_title`
- **THEN** `SubprocessMetadata.source_title is None`

### Requirement: Enrich Source metadata from fetcher chain results

The system SHALL update the existing Source row's mutable metadata — `url`, `normalized_url`, `title`, `source_type`, `content_type` — from the typed `SubprocessMetadata` produced by the conversion subprocess.

The mapping SHALL be:

- `Source.url` ← `SubprocessMetadata.source_meta.source_url`
- `Source.normalized_url` ← `SubprocessMetadata.source_meta.normalized_url`
- `Source.title` ← `SubprocessMetadata.source_title`
- `Source.source_type` ← derived from `terminal_ref.kind` via the canonical `SOURCE_TYPE_BY_KIND` mapping (unchanged)
- `Source.content_type` ← `SubprocessMetadata.content_type` (unchanged)

The Source-row enrichment write SHALL be a separate database operation from the manifest / `ConversionOutput` write; the manifest is built from `SubprocessMetadata` directly and SHALL NOT depend on the Source row.
This preserves the existing best-effort semantics: if the Source-row UPDATE fails, the manifest's authoritative values are unaffected.

All other constraints from the prior requirement (immutability of identity columns, last-writer-wins, best-effort write semantics, single-UPDATE scope, retry idempotency) SHALL be preserved.
(Previously: the requirement listed all five mutable fields without specifying their channel of origin; in practice the Source row was the implicit authoritative source for the manifest, so a failed Source UPDATE poisoned the manifest, and `url` and `title` were never populated because no metadata channel reached the worker.)

#### Scenario: Source row populated from SubprocessMetadata

- **GIVEN** a conversion completes producing `SubprocessMetadata.source_meta.source_url = "https://example.com/post"`, `source_meta.normalized_url = "https://example.com/post"`, and `source_title = "Example Post"`
- **WHEN** the worker enriches the Source row
- **THEN** `Source.url`, `Source.normalized_url`, and `Source.title` are set to those values respectively

#### Scenario: Manifest unaffected by Source enrichment failure

- **GIVEN** a conversion completes successfully but the Source-row UPDATE fails with a transient database error
- **WHEN** the manifest and `ConversionOutput` are written
- **THEN** both carry the values from `SubprocessMetadata` (not `NULL`s read back from the failed Source row), the failure is logged, and the job is marked SUCCEEDED

#### Scenario: Source title NULL when subprocess selected None

- **GIVEN** a conversion completes producing `SubprocessMetadata.source_title = None`
- **WHEN** the worker enriches the Source row
- **THEN** `Source.title` is `NULL` (no fallback to `job.title` or other identifiers)

#### Scenario: Immutable columns never rewritten

- **GIVEN** a Source row with existing `aizk_uuid`, `source_ref`, `source_ref_hash`, and `karakeep_id`
- **WHEN** the worker enriches the row after a fetch
- **THEN** those four columns are unchanged regardless of fetcher output

#### Scenario: Enrichment failure does not fail the job

- **GIVEN** a fetcher chain completes successfully but the Source-row UPDATE fails (e.g., transient database error)
- **WHEN** the worker attempts to persist the enriched metadata
- **THEN** the failure is logged with `aizk_uuid`, the column set attempted, and the underlying error, and the conversion job proceeds to completion with a valid manifest

#### Scenario: source_type derived from terminal ref kind

- **GIVEN** a fetcher chain that terminates at an `ArxivRef` (whether submitted directly or resolved from a KaraKeep bookmark)
- **WHEN** the worker enriches the Source row
- **THEN** the Source row's `source_type` column is set to the value mapped from `terminal_ref.kind` by the canonical `SOURCE_TYPE_BY_KIND` mapping defined in `aizk.conversion.core.types` (e.g., `arxiv` kind → `"arxiv"`; `github_readme` kind → `"github"`; `url`, `karakeep_bookmark`, `inline_html`, `singlefile` → `"other"`)

### Requirement: Normalize URLs for deduplication

The system SHALL compute `normalized_url` for any observed `source_url` via the `normalize_url()` function defined in the `url-utils` capability.
`SubprocessMetadata.source_meta.normalized_url` SHALL be set to `normalize_url(source_url)` whenever `source_url` is a syntactically valid URL.
When `source_url` is `None` or fails URL validation, `normalized_url` SHALL be `None` and the worker SHALL emit a debug-level log line identifying the job and the reason; the job SHALL NOT fail. (Previously: the requirement existed for deduplication but no pipeline stage actually computed a normalized URL, so `Source.normalized_url` was always `NULL`.)

#### Scenario: Normalized URL computed from valid source URL

- **GIVEN** a job whose final `source_meta.source_url` is a valid URL
- **WHEN** the subprocess builds `SubprocessMetadata`
- **THEN** `source_meta.normalized_url == normalize_url(source_url)`

#### Scenario: Normalized URL is None when source URL is missing

- **GIVEN** a job whose final `source_meta.source_url is None` (e.g. an `inline_html` submission)
- **WHEN** the subprocess builds `SubprocessMetadata`
- **THEN** `source_meta.normalized_url is None` and a debug log line records the absence

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
Content-type and source-type detection are the responsibility of the fetcher/resolver chain, not the conversion coordinator. (Previously: detection was performed by the conversion coordinator from KaraKeep bookmark structure and URL.)

#### Scenario: Fetcher returns empty content

- **GIVEN** a fetcher returns a `ConversionInput` with zero-length bytes
- **WHEN** the conversion coordinator inspects the input
- **THEN** the job is marked permanently failed with a missing-content error code

#### Scenario: No converter registered for content type

- **GIVEN** a fetcher returns a `ConversionInput` with a content type for which no converter is registered
- **WHEN** the conversion coordinator attempts to resolve a converter
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

The `GithubReadmeFetcher` SHALL extract the repository owner and name from a GitHub URL and fetch the README from the default branch, preferring Markdown over reStructuredText over plain text.

#### Scenario: GitHub README fetched and converted

- **GIVEN** a `GithubReadmeRef` for a bookmark with a valid repository URL
- **WHEN** the `GithubReadmeFetcher` fetches the README
- **THEN** the README content is fetched from the default branch and converted to Markdown

#### Scenario: GitHub repository with no README fails permanently

- **GIVEN** a `GithubReadmeRef` whose repository has no README file
- **WHEN** the `GithubReadmeFetcher` attempts to fetch the README
- **THEN** the job is marked permanently failed

### Requirement: Convert documents to Markdown and extract figures

The system SHALL convert each source document to Markdown and extract figures by invoking the converter resolved for the job's content type and the deployment's configured converter name.
Converter-specific behavior (picture classification, figure enrichment, serialization annotations) is an internal concern of the converter adapter. (Previously: Docling was invoked directly by name with inline format dispatch.)

#### Scenario: Converter resolved and invoked

- **GIVEN** a `ConversionInput` with content type `pdf` and a configured converter name `"docling"`
- **WHEN** the conversion coordinator invokes conversion
- **THEN** the `DoclingConverter` is resolved from the registry and produces `ConversionArtifacts`

#### Scenario: Converter-specific enrichment remains internal

- **GIVEN** the `DoclingConverter` is invoked for a PDF with picture classification enabled
- **WHEN** conversion completes
- **THEN** figure descriptions and classification metadata are present in the output, as before, without the conversion coordinator having knowledge of these adapter-specific behaviors

### Requirement: Route figure-description prompts by classification label

When picture classification is enabled and a picture-description endpoint is configured, the `DoclingConverter`'s post-conversion enrichment pass SHALL select each figure's description by its classification label: chart-type figures receive chart summaries, table-type figures receive tabular-form descriptions, and all other or unclassified figures receive generic alt-text.
Each resulting description SHALL be recorded as the figure's description metadata (`meta.description`).
Whether this routing runs at all is governed by the configuration in "Load picture classification configuration from environment".

#### Scenario: Chart figure described with chart2summary prompt

- **GIVEN** a PDF figure is classified as a chart type by `DocumentFigureClassifier`
- **WHEN** the post-conversion enrichment pass runs
- **THEN** the enrichment loop calls the VLM with a `<chart2summary>` prompt for that figure
- **AND** the resulting description is recorded as the figure's description metadata (`meta.description`)

#### Scenario: Table-image figure described with tables_html prompt

- **GIVEN** a PDF figure is classified as a table type by `DocumentFigureClassifier`
- **WHEN** the post-conversion enrichment pass runs
- **THEN** the enrichment loop calls the VLM with a `<tables_html>` prompt for that figure
- **AND** the resulting description is recorded as the figure's description metadata (`meta.description`)

#### Scenario: Unclassified or photo figure uses generic prompt

- **GIVEN** a PDF figure has no classification label, or is classified as photograph/logo/other
- **WHEN** the post-conversion enrichment pass runs
- **THEN** the enrichment loop calls the VLM with the existing generic alt-text prompt
- **AND** the resulting description is recorded as the figure's description metadata (`meta.description`)

### Requirement: Serialize Markdown output with figure metadata

When serializing a `PictureItem` to Markdown, the system SHALL append figure metadata blocks as HTML comments following the image placeholder, and SHALL NOT emit the serialization framework's default metadata lines (e.g. a bare `[Classification] <label>` line) for the figure.
If figure description metadata (`meta.description`) is present, the serializer emits a `<!-- Figure Description -->` comment block containing the description text.
If figure classification metadata (`meta.classification`) is also present, the serializer prepends a `<!-- Figure Type: <label> -->` comment immediately before the description block, enabling downstream consumers to filter or route by figure type.

#### Scenario: Classification label included in serialized output

- **GIVEN** a `PictureItem` has both classification and description metadata
- **WHEN** the item is serialized to Markdown
- **THEN** the output contains `<!-- Figure Type: <label> -->` followed by the description block
- **AND** the output contains no framework default metadata line (e.g. `[Classification] <label>`)

#### Scenario: No classification label when classifier disabled

- **GIVEN** `do_picture_classification=False` and no classification metadata exists
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

The system SHALL create a conversion output record capturing artifact locations, content hash, figure count, pipeline metadata, Docling version, the config snapshot used for the conversion, and the human-readable `title` on successful job completion.
`ConversionOutput.title` SHALL be set to `SubprocessMetadata.source_title` when that value is non-`None`, falling back to the parent `ConversionJob.title` placeholder when the subprocess selected no usable title; the column remains non-null and no schema migration is required for this change.
All other constraints from the prior requirement (`owner_id` copied from `Job.owner_id`, no Principal resolution by the worker, `Job.owner_id` not mutated) SHALL be preserved. (Previously: the requirement did not specify how `ConversionOutput.title` was sourced, and `_prepare_upload` read `source.title or job.title` from the Source row, which yielded the placeholder UUID whenever Source enrichment had not run.)

#### Scenario: Output title uses subprocess-selected source title

- **GIVEN** a conversion completes producing `SubprocessMetadata.source_title = "Attention Is All You Need"`
- **WHEN** the worker creates the `ConversionOutput` row
- **THEN** `ConversionOutput.title == "Attention Is All You Need"`

#### Scenario: Output title falls back to job placeholder when subprocess selected None

- **GIVEN** a conversion completes producing `SubprocessMetadata.source_title = None` and the parent `ConversionJob.title` is the submit-time placeholder (e.g. a Karakeep id or `aizk_uuid` string)
- **WHEN** the worker creates the `ConversionOutput` row
- **THEN** `ConversionOutput.title` equals that placeholder, the row insert succeeds, and the column's NOT NULL constraint is not violated

#### Scenario: Output record created after successful upload

- **GIVEN** all artifacts are uploaded and verified
- **WHEN** the worker finalizes the job
- **THEN** a conversion output record is created with S3 prefixes, bare S3 Markdown key (e.g. `{uuid}/output.md`, no `s3://` URI prefix), bare S3 manifest key (e.g. `{uuid}/manifest.json`), content hash, figure count, Docling version, pipeline name, and timestamps

#### Scenario: Output owner_id copied from parent Job

- **GIVEN** a Job with `owner_id = "self"` reaches successful completion
- **WHEN** the worker creates the conversion output record
- **THEN** the new `conversion_outputs` row has `owner_id = "self"`, matching the parent Job's owner

#### Scenario: Worker does not mutate Job.owner_id

- **GIVEN** a Job with `owner_id = "self"` is processed by the worker
- **WHEN** the worker writes any of its mutable metadata columns (status, attempt count, error message, output reference)
- **THEN** the Job's `owner_id` value is unchanged, consistent with the existing Source-identity immutability invariant

### Requirement: Transition job status atomically

The system SHALL update job status to `SUCCEEDED`, `FAILED_RETRYABLE`, or `FAILED_PERM` only after the associated S3 or error state is confirmed.
The atomicity of the status update with its transition event is provided by the generic co-commit helper; conversion retains the constraint that a conversion status is set only after the associated S3/error state is confirmed, and that the conversion outcome statuses are `SUCCEEDED`, `FAILED_RETRYABLE`, and `FAILED_PERM`.

#### Scenario: Status set to SUCCEEDED after verified upload

- **GIVEN** all S3 uploads are verified
- **WHEN** the transition commits
- **THEN** the job status is `SUCCEEDED`, a succeeded event record is co-committed, and the output record is visible to consumers

#### Scenario: Retryable error sets status to FAILED_RETRYABLE

- **GIVEN** a transient error occurs during fetch, conversion, or upload
- **WHEN** the error handler runs
- **THEN** the job status transitions to `FAILED_RETRYABLE`, a failure event record carrying the sanitized error identifiers is co-committed, and the prior attempt's event records remain observable

#### Scenario: Permanent error sets status to FAILED_PERM

- **GIVEN** a non-recoverable error (missing content, empty output, egress-policy rejection)
- **WHEN** the error handler runs
- **THEN** the job status transitions to `FAILED_PERM`, a failure event record carrying the sanitized error identifiers and the non-retryable indicator is co-committed, and the full attempt history remains observable

### Requirement: Record every ConversionJob status transition as a durable event

The system SHALL durably record every change to `ConversionJob.status` as a
transition event co-committed with the status change (the same-transaction
co-commit guarantee is provided by the generic helper).

Each event record SHALL identify the affected job, the underlying Source identity, the attempt number that the transition belongs to, the time of the transition, the event kind from a closed enumeration of conversion transition causes, the prior status (absent for the job's first observable state), the new status, and a typed structured cause appropriate to the kind.
The Source identity SHALL be recorded alongside the job identity so that audit queries spanning future processing-stage event records (chunking, embedding, etc.) can be served by Source identity without joining through `ConversionJob`.

Event records SHALL NOT be updated or deleted by the conversion system after insertion; corrections SHALL be expressed as new event records.
The `ConversionJob.status` field SHALL remain the authoritative current-state projection used by scheduling, claiming, and read paths; the event record set SHALL NOT be the source of truth for those paths.

#### Scenario: Status transition produces exactly one event atomic with the projection

- **GIVEN** a ConversionJob currently in `QUEUED`
- **WHEN** the worker claims the job and transitions it to `RUNNING`
- **THEN** the same commit that mutates `ConversionJob.status` to `RUNNING` also persists exactly one event record with prior status `QUEUED`, new status `RUNNING`, the attempt number that the new RUNNING state belongs to, and a typed cause payload identifying the claim

#### Scenario: Transition rollback leaves job in prior status with no event record

- **GIVEN** a transition whose commit fails (e.g., constraint violation, database error, or any other exception raised between mutation and commit)
- **WHEN** the surrounding transaction rolls back
- **THEN** subsequent readers observe the job in its prior status, and no event record for the failed transition is observable

#### Scenario: Retry preserves prior attempt's event records

- **GIVEN** a ConversionJob whose first attempt transitioned through `RUNNING → FAILED_RETRYABLE` with one or more recorded event records carrying the attempt-1 attempt number
- **WHEN** the job is reclaimed for a second attempt and subsequently succeeds
- **THEN** the event records for attempt 1 remain observable with the attempt-1 number, and the event records for attempt 2 are observable with the attempt-2 number, with no overwrite or deletion of prior records

#### Scenario: Initial submission event has no prior status

- **GIVEN** a new ConversionJob submitted via the API and persisted with status `QUEUED`
- **WHEN** the corresponding event record is written
- **THEN** the event record carries no prior status, new status `QUEUED`, and the kind that identifies an initial submission

#### Scenario: Permanent failure transition is recorded

- **GIVEN** a job in status `RUNNING` whose subprocess produces a non-retryable failure
- **WHEN** the stage handler transitions the job to `FAILED_PERM`
- **THEN** an event record is committed with prior status `RUNNING`, new status `FAILED_PERM`, and a typed cause payload carrying the non-retryable indicator and the sanitized error identifiers

#### Scenario: Upload-pending transition is recorded

- **GIVEN** a job whose subprocess has completed conversion successfully
- **WHEN** the stage handler transitions the job to `UPLOAD_PENDING` prior to invoking upload
- **THEN** an event record is committed with prior status `RUNNING`, new status `UPLOAD_PENDING`, and a typed cause payload identifying the artifact that is about to upload

#### Scenario: Cancellation mid-conversion is recorded

- **GIVEN** a job in status `RUNNING` whose cancellation is requested via the API
- **WHEN** the cancellation transition commits the job to `CANCELLED`
- **THEN** an event record is committed with prior status `RUNNING`, new status `CANCELLED`, and a typed cause payload carrying the cancellation context

#### Scenario: Event records are append-only

- **GIVEN** any persisted event record
- **WHEN** the worker, API, or any other component later processes the same job
- **THEN** no code path mutates or deletes the existing record; new information is expressed by appending additional event records

### Requirement: Persist subprocess-reported phase events to the event log

The system SHALL persist every `phase` event that the conversion subprocess reports to the parent process as an event record scoped to the job and attempt under which the subprocess is running.
A `phase` event reports progress within the `RUNNING` state and SHALL produce an event record without altering `ConversionJob.status`.
A failure to persist a phase event record SHALL be logged and SHALL NOT halt processing of the job; the subprocess channel is the authoritative real-time signal, and the event record set is a durable replay record.

Subprocess-reported terminal events (`completed`, `failed`, `cancelled`) SHALL drive the parent's control flow but SHALL NOT produce their own event records.
For each subprocess-driven terminal outcome, the canonical event record is the transition event committed by the parent under the "Record every ConversionJob status transition" requirement, after the parent applies all error-detail sanitization rules that constrain durable storage (including the prohibition on persisting rejected destinations from egress-policy failures).

Subprocess-reported phase event payloads SHALL be validated against the closed contract for the `phase` kind before persistence; phase reports whose payload fails validation SHALL be discarded with a log entry and SHALL NOT produce an event record.

#### Scenario: Phase event recorded without status change

- **GIVEN** a job in status `RUNNING` whose subprocess reports a recognized phase
- **WHEN** the parent drains the report
- **THEN** one event record is committed identifying the phase and the current attempt, and `ConversionJob.status` is unchanged

#### Scenario: Multiple phases for the same attempt each produce a record

- **GIVEN** a subprocess that reports two distinct phases during a single attempt
- **WHEN** the parent drains both reports
- **THEN** two distinct event records exist for that job and attempt, one per reported phase, in the order reported

#### Scenario: Subprocess-reported terminal event does not produce its own record

- **GIVEN** a subprocess that reports `failed` with a retryable error code, followed by the stage handler transitioning the job to `FAILED_RETRYABLE`
- **WHEN** the parent drains the subprocess report and the stage handler commits the transition
- **THEN** the only failure-related event record observable for that attempt is the stage-handler-committed transition event carrying the sanitized error identifiers

#### Scenario: Subprocess-reported cancellation does not produce its own record

- **GIVEN** a job whose API-side cancellation transition has already committed and whose subprocess subsequently reports `cancelled`
- **WHEN** the parent drains the subprocess report
- **THEN** no additional cancellation event record is appended; the API-side transition event is the sole cancellation record

#### Scenario: Egress-policy rejection detail not persisted via subprocess channel

- **GIVEN** a subprocess that reports `failed` with an egress-policy error code and a traceback containing a rejected destination
- **WHEN** the parent drains the report and the stage handler commits the resulting transition
- **THEN** no event record observable to durable readers carries the rejected destination; the stage handler's transition event carries only the sanitized error code

#### Scenario: Phase-event persistence failure does not halt the job

- **GIVEN** a subprocess that reports a recognized phase successfully and an event-record persistence failure during that write
- **WHEN** the parent attempts to record the phase event
- **THEN** the failure is logged with the job and attempt identifiers, the job continues processing, and the next reported phase still attempts its own persistence

#### Scenario: Phase event with unrecognized payload is dropped, not persisted

- **GIVEN** a subprocess that reports a phase whose payload fields do not match the closed contract for the `phase` kind (e.g., an unrecognized phase name or an extra unspecified field)
- **WHEN** the parent attempts to record the phase event
- **THEN** the report is logged as a validation failure and no event record is appended; the job continues processing

### Requirement: Record Source enrichment writes as events scoped to the originating job

The system SHALL record every attempted Source-row enrichment write performed by the conversion worker as an event record, identifying the originating job, the targeted Source identity, the set of mutable Source columns whose values the worker attempted to write, and whether the underlying Source UPDATE succeeded.
The event record SHALL be written whether the Source UPDATE succeeded or failed; the audit record is independent of the best-effort write semantics of Source enrichment.
The event record SHALL NOT be written for Source-row mutations that the conversion worker does not author.

#### Scenario: Successful enrichment records event with success indicator

- **GIVEN** a job whose subprocess produced source metadata and a Source UPDATE that commits successfully
- **WHEN** the worker enriches the Source row
- **THEN** an enrichment event record is appended scoped to the originating job, identifying the Source by its durable identity, the columns the worker wrote, and an indicator that the UPDATE succeeded

#### Scenario: Failed enrichment still records event with failure indicator

- **GIVEN** a job whose Source UPDATE fails after the worker attempts enrichment
- **WHEN** the failure is caught
- **THEN** an enrichment event record is still appended scoped to the originating job, identifying the columns the worker attempted to write and an indicator that the UPDATE failed, and the conversion job proceeds to completion as before

#### Scenario: No event for Source rows the worker did not author

- **GIVEN** a Source row whose mutable columns are written by code outside the conversion worker
- **WHEN** the change is committed
- **THEN** no enrichment event record is appended; the requirement applies only to writes the worker itself authors

### Requirement: Event payloads validate against typed per-kind contracts

The system SHALL define, for each kind in the closed enumeration of conversion event kinds, a typed payload contract specifying that kind's permitted fields and field types.
The generic obligation to validate every event payload against its kind's contract before insertion is provided by the runner; this requirement retains the conversion-specific per-kind payload contracts and conversion's forward-compatibility rule.

An unknown kind SHALL fail validation.
A payload whose fields do not satisfy the contract for its kind SHALL fail validation.
Readers SHALL tolerate the future addition of unrecognized fields to a known kind's payload (forward-compatibility with additive changes); incompatible payload changes (renamed fields, removed fields, changed types) SHALL be expressed by introducing a new kind to the closed enumeration rather than by mutating an existing kind's contract.

#### Scenario: Recognized kind with valid fields validates

- **GIVEN** an event with a recognized kind and a payload whose fields satisfy that kind's contract
- **WHEN** the payload is validated before insertion
- **THEN** validation succeeds and the event record is inserted

#### Scenario: Unknown kind rejected

- **GIVEN** an attempt to record an event with a kind that is not a member of the closed enumeration
- **WHEN** validation runs
- **THEN** a typed validation error is raised and no event record is inserted

#### Scenario: Extra field for a known kind rejected on write

- **GIVEN** an attempt to record an event whose payload includes a field not defined on its kind's contract
- **WHEN** validation runs
- **THEN** a typed validation error is raised and no event record is inserted

#### Scenario: Reader tolerates additive field on a previously-persisted record

- **GIVEN** a previously persisted event record whose payload was written under an older code version
- **WHEN** the current reader deserializes the payload
- **THEN** any unrecognized fields are ignored without raising, and the recognized fields are returned

### Requirement: Bound concurrency of GPU-consuming conversion phases

The system SHALL bound the number of GPU-consuming conversion subprocesses running concurrently via a GPU `ResourceGuard` context manager acquired in the parent process before subprocess spawn and held for the subprocess's full lifetime (spawn, supervision, and reap).
The guard SHALL be a threading primitive shared across the parent's worker thread pool.
The stage handler SHALL acquire the guard if and only if the dispatched converter declares `requires_gpu == True`; a converter declaring `requires_gpu == False` SHALL spawn its subprocess without contending on the GPU guard.
Converter adapters running inside forked child processes SHALL NOT own or acquire the cross-job GPU guard.
The acquiring worker thread SHALL be the sole releaser; the supervision loop SHALL NOT call release directly, and the guard SHALL be released by normal or exceptional unwind of the acquiring thread's `with` block after subprocess reap.
Today the only registered converter is `DoclingConverter` with `requires_gpu = True`, so every conversion subprocess acquires the guard in the current deployment; the bypass path is a defined protocol capability for future non-GPU converters. (Previously: bounded by a module-level `threading.Semaphore` in the conversion worker.
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

The system SHALL declare conversion's required external-service probes — S3, the database, and (when configured) the picture-description endpoint — through its registered adapters so the runner's startup-validation gate executes them before any conversion work is accepted.
The set of probes SHALL be determined by the registered adapters and their configuration.

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
Version 2.0 manifests SHALL carry — in addition to the previously-required `config_snapshot`, `submitted_ref`, and `terminal_ref` blocks — the manifest source-block fields `url`, `normalized_url`, and `title`, all read directly from `SubprocessMetadata`:

- `manifest.source.url` ← `SubprocessMetadata.source_meta.source_url`
- `manifest.source.normalized_url` ← `SubprocessMetadata.source_meta.normalized_url`
- `manifest.source.title` ← `SubprocessMetadata.source_title`

Each field SHALL be `null` in the manifest when the corresponding `SubprocessMetadata` value is `None`.
The manifest SHALL NOT read these fields from the Source row; the manifest is the authoritative record for the job and must be independent of the best-effort Source cache.
Field names on the manifest source block SHALL remain `url` / `normalized_url` / `title` (no schema rename); only the source of those values changes.
All other constraints from the prior requirement (manifest version dispatch, `extra="forbid"` config snapshot, ref block semantics, nullable v1.0 → v2.0 fields) SHALL be preserved. (Previously: the manifest had no defined channel for source-descriptive fields the converter or fetcher observed, and `_prepare_upload` constructed the manifest by reading `source.url` / `source.title or job.title` from the Source row, which made the authoritative manifest contingent on the best-effort enrichment write succeeding.)

#### Scenario: Manifest carries observed source URL and selected title

- **GIVEN** a conversion completes for a job whose final `SubprocessMetadata.source_meta.source_url = "https://example.com/post"`, `normalized_url = "https://example.com/post"`, and `source_title = "Example Post"`
- **WHEN** the manifest is written
- **THEN** the manifest's source block carries `url = "https://example.com/post"`, `normalized_url = "https://example.com/post"`, and `title = "Example Post"`

#### Scenario: Manifest source fields null when nothing observed

- **GIVEN** a conversion completes for an `inline_html` job where `SubprocessMetadata.source_meta.source_url = None`, `normalized_url = None`, and `source_title = None`
- **WHEN** the manifest is written
- **THEN** the manifest's source block carries `url = null`, `normalized_url = null`, and `title = null`

#### Scenario: Manifest values independent of Source-row state

- **GIVEN** a conversion completes successfully and the Source-row UPDATE then fails
- **WHEN** the manifest is generated and written
- **THEN** the manifest's `url`, `normalized_url`, and `title` match the values in `SubprocessMetadata` regardless of what `Source.url` / `Source.title` currently hold (including `NULL`)

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

### Requirement: Containment-check subprocess artifact metadata

The conversion subprocess produces a metadata record describing where the converted Markdown and any extracted figures were written within the job's workspace.
The parent process consumes those filenames to read and upload artifacts.
The system SHALL treat the boundary between the conversion subprocess and the parent process as a trust boundary: every filename the parent receives from subprocess-produced metadata SHALL be containment-checked against the job's workspace directory before the parent reads, opens, or uploads the file.

A filename received from subprocess metadata SHALL be rejected if any of the following hold:

- It is an absolute path.
- It contains a path separator (`/` or `\`) or a parent-traversal segment (`..`).
- The path obtained by composing it with the workspace directory and resolving symlinks does not lie within the resolved workspace directory.

Rejection SHALL surface as a typed error and SHALL fail the job as a non-retryable failure; the parent SHALL NOT read or upload the rejected path.

#### Scenario: Standard subprocess-produced filename accepted

- **GIVEN** the conversion subprocess records `markdown_filename = "output.md"` and `figure_files = ["figure-001.png", "figure-002.png"]` in metadata
- **WHEN** the parent prepares to read and upload the artifacts
- **THEN** every filename passes the containment check, the artifacts are read from inside the workspace, and the upload proceeds

#### Scenario: Parent-traversal filename rejected

- **GIVEN** the conversion subprocess records `markdown_filename = "../../etc/hostname"` in metadata
- **WHEN** the parent prepares to read the markdown artifact
- **THEN** a typed containment error is raised, no `open()` is issued against the traversal path, the job is failed as non-retryable, and no upload is performed

#### Scenario: Absolute-path filename rejected

- **GIVEN** the conversion subprocess records `markdown_filename = "/etc/hostname"` in metadata
- **WHEN** the parent prepares to read the markdown artifact
- **THEN** a typed containment error is raised regardless of whether the path exists; the parent SHALL NOT compute `workspace / "/etc/hostname"` and read the resulting absolute path

#### Scenario: Backslash-separator filename rejected

- **GIVEN** the conversion subprocess records `figure_files = ["..\\..\\etc\\hostname"]` in metadata
- **WHEN** the parent prepares to read the figure artifacts
- **THEN** a typed containment error is raised because the filename contains a path separator

#### Scenario: Symlink-escape filename rejected

- **GIVEN** a workspace contains a symlink `escape -> /etc` and the conversion subprocess records `figure_files = ["escape/hostname"]` in metadata
- **WHEN** the parent prepares to read the figure
- **THEN** the resolved path falls outside the resolved workspace directory and the containment check rejects the filename

## Technical Notes

- **Implementation**: `aizk/conversion/`
- **Dependencies**: conversion-api (shared data model); mlflow-llm-tracing (optional instrumentation); worker-process-management (subprocess lifecycle)
- **Data model**: Bookmark (karakeep_id unique key, source_id internal identifier), ConversionJob (status, idempotency_key unique, retry scheduling), ConversionOutput (artifact locations, content hash, pipeline metadata)
- **Idempotency key**: hash of `source_id + payload_version + docling_version + config_hash + picture_description_enabled`; `config_hash` covers the converter output-affecting fields (e.g., OCR, table structure, page limit, picture timeout, picture description model, `picture_classification_enabled`) and excludes provider identity and credentials (`picture_description_base_url`, `picture_description_api_key`)
- **Raw source provenance**: KaraKeep is the authoritative store for raw source content; `karakeep_id` is the durable provenance reference.
  Raw bytes are not archived locally.
- **Whitespace normalization**: `aizk/conversion/utilities/whitespace.py` → `normalize_whitespace()`; applied in `_run_conversion()` before file write and hash computation; preserves code-fence content, list indentation, and strips trailing spaces
- **Content hash**: xxhash64 of normalized Markdown (UTF-8, LF line endings) stored in the output record
- **Manifest config_snapshot**: manifest includes a `config_snapshot` section with the Docling config fields that affect replayable output (including `picture_classification_enabled`) and `picture_description_enabled` to enable exact replay; provider identity and credentials are excluded, and secrets are never persisted to durable artifact storage.
  The snapshot contract is enforced by `ManifestConfigSnapshot` (pydantic model with `extra="forbid"`) in `aizk/conversion/storage/manifest.py`.
- **S3 layout**: artifacts at `s3://<bucket>/<source_id>/`; upload verification via ETag match (single-part) or content length (multipart)
- **Database**: SQLite in WAL mode with foreign keys, synchronous=NORMAL, and busy timeout; indexes on job status/scheduling, bookmark URL, and output lookup by bookmark identifier
- **Workspace**: ephemeral temp directory cleaned up via context manager; figures named figure-001.png, figure-002.png, etc.
- **arXiv client**: `aizk.utilities.arxiv_utils`; URL parsing: `aizk.utilities.url_utils`
- **Process role labeling**: implemented via `setproctitle`
- **Startup validation**: `aizk/conversion/utilities/startup.py` → `validate_startup()`; probes S3 (HEAD bucket), KaraKeep (GET bookmarks?limit=1), and — when configured — picture description endpoint (GET /models) with 10s timeouts; logs feature summary for picture descriptions, picture classification, MLflow, and Litestream
- **Picture classification**: `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED` (default: `True`); controls `ThreadedPdfPipelineOptions.do_picture_classification` and the post-conversion enrichment loop in `converter.py`; prompt routing table `_LABEL_TO_PROMPT` maps classifier labels to `<chart2summary>` / `<tables_html>` / generic alt-text
