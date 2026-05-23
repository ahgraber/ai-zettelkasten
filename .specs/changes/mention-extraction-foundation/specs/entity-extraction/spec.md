# Delta for entity-extraction

## ADDED Requirements

### Requirement: Each reified chunk yields its identified mentions within the run, with uniform versions

For any chunk processed by extraction within a reification run, every entity mention identified in that chunk SHALL be persisted as a mention record belonging to that run and sourced to that chunk.
All mentions emitted within a single run SHALL carry the same extractor and reifier versions as the run records.

#### Scenario: Identified mentions are persisted under the run and sourced to the chunk

- **GIVEN** a chunk processed by extraction within a reification run, containing identifiable entity mentions
- **WHEN** extraction completes for that chunk
- **THEN** each identified mention is persisted as a record belonging to that run whose `chunk_id` is the chunk's id

#### Scenario: One run stamps one set of versions

- **GIVEN** an extraction run that emits mentions from multiple chunks
- **WHEN** the emitted mentions' versions are compared
- **THEN** they all equal the run's recorded extractor and reifier versions

### Requirement: Extraction emits an input span and a raw-chunk anchor span for every mention

For every mention it emits, extraction SHALL record an `input_span` indexing the text it actually read and a `source_chunk_span` anchoring the mention into the raw chunk text.
When extraction reads a contextualized variant in which a reference was resolved, the `source_chunk_span` SHALL anchor to the referring expression in the raw chunk and the `surface_form` SHALL be the resolved form.

#### Scenario: A verbatim mention's two spans coincide on the same text

- **GIVEN** extraction reading raw chunk text and emitting a mention whose surface form appears verbatim
- **WHEN** the mention's `input_span` and `source_chunk_span` are resolved against the raw chunk
- **THEN** both resolve to the mention's surface form in the raw chunk

#### Scenario: A resolved-reference mention maps its input span back to a raw anchor

- **GIVEN** extraction reading a contextualized variant where a pronoun was resolved to an explicit referent
- **WHEN** the resulting mention's spans are inspected
- **THEN** `input_span` indexes the resolved form in the variant while `source_chunk_span` anchors to the referring expression in the raw chunk

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

### Requirement: Blocking keys are a deterministic function of the surface form

A mention's blocking keys SHALL be derived deterministically from its surface form, such that two mentions with the same surface form receive the same blocking keys and the key set serves as a stable candidate-generation index.

#### Scenario: Equal surface forms yield equal blocking keys

- **GIVEN** two mentions with identical surface forms
- **WHEN** their blocking keys are compared
- **THEN** the key sets are equal

#### Scenario: Blocking keys are reproducible across runs

- **GIVEN** a surface form extracted in two separate runs of the same extractor version
- **WHEN** the blocking keys from each run are compared
- **THEN** they are identical

### Requirement: Extraction reads the variant from the active contextualization run and records the input used

When a chunk has a contextualized variant in its document's **active** contextualization run, extraction SHALL read that variant and record `input_kind` as contextualized with `input_ref` identifying that variant; superseded variants SHALL NOT be read.
Otherwise extraction SHALL read the chunk's raw text and record `input_kind` as raw with `input_ref` identifying the chunk.

#### Scenario: A chunk with an active-run variant is extracted from it

- **GIVEN** a chunk whose document has a contextualized variant in its active contextualization run
- **WHEN** extraction processes the chunk
- **THEN** extraction reads that variant and the resulting mentions record contextualized `input_kind` and the variant as `input_ref`

#### Scenario: A chunk with no active variant falls back to raw text

- **GIVEN** a chunk that has no contextualized variant in an active run (none produced, or only superseded ones exist)
- **WHEN** extraction processes the chunk
- **THEN** extraction reads the raw chunk text and the resulting mentions record raw `input_kind` and the chunk as `input_ref`

### Requirement: Extraction output is independent of run mode

For any chunk, a reification run SHALL produce the same mentions and the same co-occurrences whether the chunk is processed in bulk/backfill mode or incremental mode under the same versions and inputs.
Run mode SHALL affect only batching and scheduling, never which mentions or co-occurrences are produced.

#### Scenario: Bulk and incremental extraction produce the same mentions

- **GIVEN** a chunk extracted in bulk/backfill mode and the same chunk extracted in incremental mode within runs of the same versions and inputs
- **WHEN** the persisted mentions and their co-occurrences are compared
- **THEN** both yield the same mentions (equal `source_occurrence_key`s) and the same co-occurrence links
