# Delta for Conversion Worker

## ADDED Requirements

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

## MODIFIED Requirements

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
