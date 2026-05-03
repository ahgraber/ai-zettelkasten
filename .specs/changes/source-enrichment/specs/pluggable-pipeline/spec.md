# Delta for Pluggable Pipeline

## ADDED Requirements

### Requirement: Carry source observations as a typed metadata channel through the pipeline

The system SHALL define a `SourceMetadata` value type that propagates source-descriptive fields — initially `source_url: str | None`, `normalized_url: str | None`, `document_base_url: str | None`, and `resolver_title: str | None` — from the resolver stage through the fetcher and converter stages without loss.

`SourceMetadata` SHALL be an immutable value type with optional fields, addable without breaking existing adapters.

A `SourceMetadata` value SHALL exist for every job from the resolver stage onward; when the job has no resolver, the orchestrator SHALL synthesise a `SourceMetadata()` with all fields `None` before invoking the fetcher.

`SourceMetadata` SHALL define an explicit `merge(other) -> SourceMetadata` operation with field-wise "earlier non-None wins" semantics: for each field, the result is `self.<field>` if non-`None`, else `other.<field>`.
This SHALL be the only mechanism by which a stage combines its observations with prior observations.

A pipeline stage SHALL NOT silently overwrite a non-`None` `SourceMetadata` field with a `None` or replacement value via direct assignment; enrichment goes through `merge()`.

#### Scenario: Metadata exists for non-resolver job

- **GIVEN** a job submitted with a `SourceRef` whose kind has no registered resolver
- **WHEN** the orchestrator dispatches to the fetcher
- **THEN** a `SourceMetadata` value with all fields `None` is passed alongside the ref

#### Scenario: Resolver observation reaches the converter unchanged

- **GIVEN** a resolver returns a `SourceMetadata` with `source_url`, `document_base_url`, and `resolver_title` set
- **WHEN** the fetcher and converter run
- **THEN** the converter observes the same values for those three fields on `input.source_meta`

#### Scenario: Merge preserves earlier non-None fields

- **GIVEN** a `SourceMetadata` with `source_url = "https://a"` and `resolver_title = None`, merged with another carrying `source_url = "https://b"` and `resolver_title = "T"`
- **WHEN** `merge()` is called
- **THEN** the result carries `source_url = "https://a"` and `resolver_title = "T"`

#### Scenario: Optional fields extend without breaking adapters

- **GIVEN** a new optional field is added to `SourceMetadata`
- **WHEN** an existing adapter that does not set the new field runs
- **THEN** the field is `None` and no adapter behaviour changes

### Requirement: Define a typed subprocess metadata schema for the conversion IPC bridge

The system SHALL define a typed `SubprocessMetadata` model that is the sole wire format for `metadata.json` written by the conversion subprocess and read by the parent process.
The model SHALL carry, at minimum: `pipeline_name`, `terminal_ref`, `content_type`, artifact filenames (`markdown_filename`, `figure_files`), pipeline/converter version (`docling_version`, `config_snapshot`), the final `source_meta: SourceMetadata` observed during conversion, the raw `document_title: str | None` returned by the converter, and the selected `source_title: str | None` chosen per the title-selection policy in the conversion-worker spec.

The subprocess SHALL serialise `SubprocessMetadata` to `metadata.json`; the parent SHALL deserialise the same type.
Both sides SHALL reject unknown fields (`extra="forbid"`-equivalent semantics).

`SubprocessMetadata.source_title` SHALL be the authoritative value for the manifest and `ConversionOutput`; downstream consumers SHALL NOT re-derive it from the Source row.

#### Scenario: Subprocess writes typed metadata

- **GIVEN** the conversion subprocess has produced artifacts and selected a `source_title`
- **WHEN** it writes `metadata.json`
- **THEN** the file contains a serialised `SubprocessMetadata` carrying `source_meta`, `document_title`, and `source_title` alongside the existing fields

#### Scenario: Parent rejects malformed subprocess metadata

- **GIVEN** the parent reads a `metadata.json` whose payload contains an unknown field or a missing required field
- **WHEN** it deserialises into `SubprocessMetadata`
- **THEN** a typed validation error is raised and the job fails non-retryably

## MODIFIED Requirements

### Requirement: Declare content fetching as a protocol with two roles

The system SHALL support two fetcher roles — content fetchers and ref resolvers — each with a distinct protocol.
A content fetcher SHALL accept a `SourceRef` and a `SourceMetadata` value, and return a `ConversionInput` containing the fetched bytes, authoritative content type, and a (possibly enriched) `SourceMetadata`.
A ref resolver SHALL accept a `SourceRef` and return a tuple of `(SourceRef, SourceMetadata)` — a more specific `SourceRef` and any source-descriptive fields it observed during resolution — deferring byte-level fetching to a downstream content fetcher.
A `SourceRef` SHALL remain a pure identity / fetch-instruction value (used for hashing, registry dispatch, and `source_ref_hash`); source-descriptive fields SHALL flow exclusively through `SourceMetadata`, not as fields on `SourceRef` variants.
All other constraints from the prior requirement (class-level `produces` / `resolves_to`, structural role detection via `@runtime_checkable`, registration-time and dispatch-time isinstance checks, no class-level submittability flag) SHALL be preserved. (Previously: content fetchers accepted only a `SourceRef`; ref resolvers returned only a `SourceRef`.
Source-descriptive metadata observed during resolution had no return path and was discarded.)

#### Scenario: Content fetcher receives and returns SourceMetadata

- **GIVEN** a `SourceRef` whose kind maps to a registered content fetcher and a `SourceMetadata` from the orchestrator
- **WHEN** the fetcher is invoked
- **THEN** a `ConversionInput` is returned whose `source_meta` field carries forward all non-`None` fields supplied by the caller, plus any fields the fetcher itself observed, combined via `SourceMetadata.merge()`

#### Scenario: Ref resolver returns refined ref with metadata

- **GIVEN** a `SourceRef` whose kind maps to a registered ref resolver
- **WHEN** the resolver is invoked
- **THEN** a `(SourceRef, SourceMetadata)` tuple is returned where the ref is of a different kind and the metadata holds any source-descriptive fields the resolver observed (or all-`None` fields if none were observed)

#### Scenario: SourceRef carries no descriptive fields

- **GIVEN** a `SourceRef` variant of any kind
- **WHEN** its public field set is inspected
- **THEN** it contains only identity / fetch-instruction fields (e.g. `bookmark_id`, `url`, `arxiv_id`); descriptive fields such as a canonical display URL or a human title appear only on `SourceMetadata`

### Requirement: Declare document conversion as a capability-indexed protocol

The system SHALL support a converter protocol where each converter implementation declares the set of content types it can handle and whether it requires GPU admission control.
A converter SHALL accept a `ConversionInput` and return `ConversionArtifacts` containing the converted output, any extracted assets, and a `document_title: str | None` reflecting the document's own title when one is present in the source.
The converter SHALL return `document_title` as a raw observation and SHALL NOT apply title-selection policy (e.g. fallback to resolver title); selection across `document_title` and `source_meta.resolver_title` is owned by the conversion-worker layer.
All other constraints from the prior requirement (static `supported_formats`, static `requires_gpu`) SHALL be preserved. (Previously: `ConversionArtifacts` carried only converted output and extracted assets; document-level metadata observed during conversion had no return path and was discarded.)

#### Scenario: Converter emits raw document title when present

- **GIVEN** a `ConversionInput` whose source contains a document-level title
- **WHEN** the converter is invoked
- **THEN** the returned `ConversionArtifacts.document_title` is the document's title verbatim, with no UUID/URL filtering or fallback applied

#### Scenario: Converter emits null document title when source has none

- **GIVEN** a `ConversionInput` whose source contains no document-level title
- **WHEN** the converter is invoked
- **THEN** the returned `ConversionArtifacts.document_title` is `None` regardless of any value present in `input.source_meta.resolver_title`
