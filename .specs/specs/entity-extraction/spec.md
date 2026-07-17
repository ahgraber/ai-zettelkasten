# Entity-extraction Specification

> Synced from change `mention-extraction-foundation` on 2026-07-16

## Purpose

The entity-extraction capability runs NER over each chunk's selected input — the active contextualized variant when one exists, the raw chunk text otherwise — and materializes the detections into mention records within a per-source extraction run.
Each detection carries the span of the text actually read and a deterministic anchor class assigned by exact-match search of the raw chunk: one source-anchored mention per raw occurrence, or one revision-anchored mention when the surface exists only because the revision resolved it.
Mentions sharing a chunk are linked by intra-chunk co-occurrence.
The extractor is a substitutable dependency reached through a single access point, and the persisted output is identical whether extraction runs in bulk/backfill or incremental mode.

## Requirements

### Requirement: Each extracted chunk yields its identified mentions within the run, with uniform versions

For any chunk processed by extraction within an extraction run, every entity mention identified in that chunk SHALL be persisted as a mention record belonging to that run and sourced to that chunk.
All mentions emitted within a single run SHALL carry the same extractor and materializer versions as the run records.

#### Scenario: Identified mentions are persisted under the run and sourced to the chunk

- **GIVEN** a chunk processed by extraction within an extraction run, containing identifiable entity mentions
- **WHEN** extraction completes for that chunk
- **THEN** each identified mention is persisted as a record belonging to that run whose `chunk_id` is the chunk's id

#### Scenario: One run stamps one set of versions

- **GIVEN** an extraction run that emits mentions from multiple chunks
- **WHEN** the emitted mentions' versions are compared
- **THEN** they all equal the run's recorded extractor and materializer versions

### Requirement: Extraction emits an input span and a deterministic anchor class for every mention

For every mention it emits, extraction SHALL record an `input_span` indexing the text it actually read and an `anchor_kind` classifying the mention's raw anchor as source or revision.
For each detected surface form, extraction SHALL search the raw chunk text for occurrences of that surface form: when one or more occurrences exist, extraction SHALL emit one source-anchored mention per occurrence, each carrying that occurrence's `source_chunk_span`; when none exist, extraction SHALL emit one revision-anchored mention with no `source_chunk_span`.
Classification SHALL be deterministic in the raw chunk text and the detected surface form alone; extraction SHALL NOT assign detections to occurrences positionally or by fuzzy matching.

#### Scenario: A source-anchored mention's two spans coincide on the same text

- **GIVEN** extraction reading raw chunk text and emitting a mention whose surface form appears once
- **WHEN** the mention's `input_span` and `source_chunk_span` are resolved against the raw chunk
- **THEN** both resolve to the mention's surface form in the raw chunk

#### Scenario: A revision-resolved name absent from the raw chunk is emitted as revision-anchored

- **GIVEN** extraction reading a contextualized variant whose revision resolves a reference into an entity name that does not occur in the raw chunk
- **WHEN** the extractor identifies that entity
- **THEN** extraction emits one revision-anchored mention for it, carrying its `input_span` and no `source_chunk_span`

#### Scenario: A surface form repeated in the raw chunk expands to one mention per occurrence

- **GIVEN** extraction detecting a surface form that occurs at several positions in the raw chunk text
- **WHEN** mentions are emitted for that chunk
- **THEN** one source-anchored mention is emitted per occurrence, each with the `source_chunk_span` of its own occurrence

### Requirement: Co-occurrence links are intra-chunk, symmetric, and exclude self

For any two distinct mentions extracted from the same chunk within a run, each SHALL be resolvable as a co-occurrence of the other.
A mention SHALL NOT co-occur with itself, and extraction SHALL NOT link mentions originating from different chunks.

#### Scenario: Two mentions in one chunk co-occur mutually

- **GIVEN** a chunk from which extraction emits two distinct mentions within a run
- **WHEN** the co-occurrences of each are resolved
- **THEN** each returns the other, and neither returns itself

#### Scenario: A lone mention in a chunk has no co-occurrences

- **GIVEN** a chunk from which extraction emits exactly one mention
- **WHEN** that mention's co-occurrences are resolved
- **THEN** the result is empty

#### Scenario: Mentions in different chunks do not co-occur

- **GIVEN** two mentions extracted from two different chunks
- **WHEN** their co-occurrences are resolved
- **THEN** neither returns the other

### Requirement: Extraction reads the variant from the active contextualization run and records the input used

When a chunk has a contextualized variant in its document's **active** contextualization run, extraction SHALL read that variant and record `input_kind` as contextualized with `input_ref` identifying that variant; superseded variants SHALL NOT be read.
A variant that is present but empty is the already-self-contained case — its consumed contextualized text equals the raw chunk text — so for such a chunk extraction SHALL read the raw chunk text and record `input_kind` as raw with `input_ref` identifying the chunk.
Otherwise extraction SHALL read the chunk's raw text and record `input_kind` as raw with `input_ref` identifying the chunk.

#### Scenario: A chunk with an active-run variant is extracted from it

- **GIVEN** a chunk whose document has a contextualized variant in its active contextualization run
- **WHEN** extraction processes the chunk
- **THEN** extraction reads that variant and the resulting mentions record contextualized `input_kind` and the variant as `input_ref`

#### Scenario: A chunk with no active variant falls back to raw text

- **GIVEN** a chunk that has no contextualized variant in an active run (none produced, or only superseded ones exist)
- **WHEN** extraction processes the chunk
- **THEN** extraction reads the raw chunk text and the resulting mentions record raw `input_kind` and the chunk as `input_ref`

#### Scenario: A present-empty variant is consumed as raw text

- **GIVEN** a chunk whose variant in the active contextualization run is present but empty because the chunk was already self-contained
- **WHEN** extraction processes the chunk
- **THEN** extraction reads the raw chunk text and the resulting mentions record raw `input_kind` and the chunk as `input_ref`

### Requirement: Extraction output is independent of run mode

For any chunk, an extraction run SHALL produce the same mentions and the same co-occurrences whether the chunk is processed in bulk/backfill mode or incremental mode under the same versions and inputs.
Run mode SHALL affect only batching and scheduling, never which mentions or co-occurrences are produced.

#### Scenario: Bulk and incremental extraction produce the same mentions

- **GIVEN** a chunk extracted in bulk/backfill mode and the same chunk extracted in incremental mode within runs of the same versions and inputs
- **WHEN** the persisted mentions and their co-occurrences are compared
- **THEN** both yield the same mentions (equal `source_occurrence_key`s for source-anchored mentions; equal `(chunk_id, surface_form)` for revision-anchored ones) and the same co-occurrence links

### Requirement: The entity extractor is a substitutable dependency

Extraction SHALL access the extractor that identifies entity mentions as a substitutable dependency reached through a single access point.
A substitute extractor — for example, a deterministic test double returning known spans — SHALL be usable in place of the production extractor without changing extraction's logic or any other requirement in this spec.
Every extractor invocation the stage makes SHALL pass through that single access point.

#### Scenario: A substitute extractor is used without changing stage logic

- **GIVEN** a deterministic substitute extractor supplied in place of the production extractor
- **WHEN** extraction processes a chunk
- **THEN** the mentions and co-occurrences are produced using the substitute, with no change to extraction logic and with the record shape, spans, and provenance the other requirements specify

#### Scenario: Every extractor invocation passes through the single access point

- **GIVEN** extraction configured with an extractor that records each invocation it receives
- **WHEN** a set of chunks is processed
- **THEN** every extractor invocation the stage makes is observed at that access point, and the stage performs none outside it
