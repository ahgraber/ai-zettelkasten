# Delta for conversion-worker

## MODIFIED Requirements

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

## ADDED Requirements

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
