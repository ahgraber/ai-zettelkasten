# Delta for mention-store

## ADDED Requirements

### Requirement: Mentions are append-only and invalidated at the run level

A persisted mention SHALL NOT be modified or deleted.
Each mention SHALL belong to exactly one reification run, and a run SHALL record the versions and inputs that produced it (at minimum the extractor version, the reifier version, the input policy, and the contextualization input fingerprint) together with a `status` of active or superseded.
At most one reification run SHALL be active for the corpus at a time; re-reification SHALL open a new run and supersede the prior one, expressed only as a run `status` transition — prior mentions SHALL remain present and unmodified.

#### Scenario: A persisted mention is never mutated

- **GIVEN** a mention persisted under a reification run
- **WHEN** the corpus is re-reified
- **THEN** the original mention record remains present and unchanged

#### Scenario: Re-reification opens a superseding run and retains prior mentions

- **GIVEN** an active reification run with persisted mentions
- **WHEN** a new reification run is produced (changed extractor, reifier, input policy, or contextualization inputs)
- **THEN** the new run becomes active, the prior run is marked superseded, and the prior run's mentions remain present and unmodified

### Requirement: Mention identity is run-scoped with a stable cross-run occurrence key

A mention's `mention_id` SHALL be a deterministic function of `(run_id, chunk_id, source_chunk_span, surface_form)`.
Each mention SHALL also carry a `source_occurrence_key` that is a deterministic function of `(chunk_id, source_chunk_span, source_anchor_text)` and is therefore stable across runs for the same source occurrence.
Persisting a mention whose `mention_id` already exists SHALL NOT create a duplicate.

#### Scenario: The same occurrence in two runs gets distinct ids but one occurrence key

- **GIVEN** the same source occurrence (same `chunk_id`, `source_chunk_span`, and anchor text) reified under two different runs
- **WHEN** the two persisted mentions are compared
- **THEN** their `mention_id`s differ (each scoped to its run) while their `source_occurrence_key`s are equal

#### Scenario: Re-running the same reification does not duplicate within a run

- **GIVEN** a chunk reified within a run
- **WHEN** the same chunk is reified again within that same run
- **THEN** no duplicate mention record is created

### Requirement: Every mention carries complete lexical provenance with declared span coordinates and no embedding

Every persisted mention SHALL carry, populated and non-null: `surface_form`, `chunk_id`, `source_chunk_span` (character offsets into the raw chunk text), `input_kind` (raw or contextualized), `input_ref` (the input text the mention was read from), `input_span` (character offsets into that input text), and `blocking_keys`.
The `source_chunk_span` SHALL locate the mention's source anchor within the raw chunk; for a mention derived from a resolved reference, the anchor SHALL be the referring expression in the raw chunk while `surface_form` is the resolved form.
A mention SHALL NOT carry a stored context embedding; any embedding a consumer needs is recomputed on demand from the mention's chunk and span and is never persisted.

#### Scenario: A persisted mention has all provenance fields populated and no embedding

- **GIVEN** any mention emitted by extraction and persisted
- **WHEN** the mention record is inspected
- **THEN** `surface_form`, `chunk_id`, `source_chunk_span`, `input_kind`, `input_ref`, `input_span`, and `blocking_keys` are present and non-null, and no context-embedding field is stored

#### Scenario: A verbatim mention's source span resolves to its surface form

- **GIVEN** a mention whose surface form appears verbatim in the raw chunk
- **WHEN** its `source_chunk_span` is resolved against the raw chunk text
- **THEN** the resolved region equals the mention's `surface_form`

#### Scenario: A resolved-reference mention anchors to the referring expression

- **GIVEN** a mention produced from a contextualized chunk where a reference (such as a pronoun) was resolved to an explicit referent
- **WHEN** its `source_chunk_span` is resolved against the raw chunk text and its `surface_form` is inspected
- **THEN** the span locates the original referring expression in the raw chunk while `surface_form` holds the resolved form, and `input_kind` is contextualized

### Requirement: Co-occurrence is resolvable without being stored on the mention row

For any mention, its intra-chunk co-occurrences SHALL be resolvable from the store.
Co-occurrence SHALL NOT be a required stored field on the mention row.

#### Scenario: A mention's co-occurrences are resolvable

- **GIVEN** a chunk that produced two or more mentions in a run
- **WHEN** the co-occurrences of one of those mentions are queried
- **THEN** the other same-chunk mentions of that run are returned, without reading a co-occurrence field on the mention row

### Requirement: A mention's source chunk is resolvable

Every mention's `chunk_id` SHALL reference a persisted chunk, so a mention can always be traced back to the chunk it was extracted from.

#### Scenario: A mention resolves to an existing chunk

- **GIVEN** a persisted mention
- **WHEN** its `chunk_id` is looked up
- **THEN** the referenced chunk record exists in the store
