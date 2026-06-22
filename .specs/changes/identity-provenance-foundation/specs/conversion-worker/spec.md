# Delta for conversion-worker

## MODIFIED Requirements

### Requirement: Enrich Source metadata from fetcher chain results

The system SHALL update the existing Source row's mutable metadata — `url`, `normalized_url`, `title`, `source_type`, `content_type` — from the typed `SubprocessMetadata` produced by the conversion subprocess.

Serves: coherent-pipeline-foundation

> Previously: the durable source identity was named `aizk_uuid`.

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

- **GIVEN** a Source row with existing `source_id`, `source_ref`, `source_ref_hash`, and `karakeep_id`
- **WHEN** the worker enriches the row after a fetch
- **THEN** those four columns are unchanged regardless of fetcher output

#### Scenario: Enrichment failure does not fail the job

- **GIVEN** a fetcher chain completes successfully but the Source-row UPDATE fails (e.g., transient database error)
- **WHEN** the worker attempts to persist the enriched metadata
- **THEN** the failure is logged with `source_id`, the column set attempted, and the underlying error, and the conversion job proceeds to completion with a valid manifest

#### Scenario: source_type derived from terminal ref kind

- **GIVEN** a fetcher chain that terminates at an `ArxivRef` (whether submitted directly or resolved from a KaraKeep bookmark)
- **WHEN** the worker enriches the Source row
- **THEN** the Source row's `source_type` column is set to the value mapped from `terminal_ref.kind` by the canonical `SOURCE_TYPE_BY_KIND` mapping defined in `aizk.conversion.core.types` (e.g., `arxiv` kind → `"arxiv"`; `github_readme` kind → `"github"`; `url`, `karakeep_bookmark`, `inline_html`, `singlefile` → `"other"`)

### Requirement: Create a conversion output record on success

The system SHALL create a conversion output record capturing artifact locations, content hash, figure count, pipeline metadata, Docling version, the config snapshot used for the conversion, and the human-readable `title` on successful job completion.
`ConversionOutput.title` SHALL be set to `SubprocessMetadata.source_title` when that value is non-`None`, falling back to the parent `ConversionJob.title` placeholder when the subprocess selected no usable title; the column remains non-null and no schema migration is required for this change.
All other constraints from the prior requirement (`owner_id` copied from `Job.owner_id`, no Principal resolution by the worker, `Job.owner_id` not mutated) SHALL be preserved. (Previously: the requirement did not specify how `ConversionOutput.title` was sourced, and `_prepare_upload` read `source.title or job.title` from the Source row, which yielded the placeholder UUID whenever Source enrichment had not run.)

Serves: coherent-pipeline-foundation

> Previously: the durable source identity was named `aizk_uuid`.

#### Scenario: Output title uses subprocess-selected source title

- **GIVEN** a conversion completes producing `SubprocessMetadata.source_title = "Attention Is All You Need"`
- **WHEN** the worker creates the `ConversionOutput` row
- **THEN** `ConversionOutput.title == "Attention Is All You Need"`

#### Scenario: Output title falls back to job placeholder when subprocess selected None

- **GIVEN** a conversion completes producing `SubprocessMetadata.source_title = None` and the parent `ConversionJob.title` is the submit-time placeholder (e.g. a Karakeep id or `source_id` string)
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
