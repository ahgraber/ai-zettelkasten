# Delta for mention-store

## ADDED Requirements

### Requirement: Mentions are append-only and invalidated at the run level

A persisted mention SHALL NOT be modified or deleted.
Each mention SHALL belong to exactly one reification run, and a run SHALL record the versions and inputs that produced it (at minimum the extractor version, the reifier version, the input policy, and a deterministic derivation key over the consumed upstream inputs and producing versions that changes whenever any of them changes) together with a `status` of active or superseded.
At most one reification run SHALL be active per source at a time, and the corpus mention dataset SHALL be resolvable as the union of the sources' active runs; re-reifying a source SHALL open a new run and supersede that source's prior run, expressed only as a run `status` transition — prior mentions SHALL remain present and unmodified.
A reification run SHALL become observable only together with its mentions: a failed or interrupted reification SHALL leave no newly-active run and no readable mentions for its source.

Serves: replayable-duplicate-free-dataset

#### Scenario: A persisted mention is never mutated

- **GIVEN** a mention persisted under a reification run
- **WHEN** the mention's source is re-reified
- **THEN** the original mention record remains present and unchanged

#### Scenario: Re-reification opens a superseding run and retains prior mentions

- **GIVEN** a source with an active reification run and persisted mentions
- **WHEN** a new reification run is produced for that source (changed extractor, reifier, input policy, or contextualization inputs)
- **THEN** the new run becomes active, the prior run is marked superseded, and the prior run's mentions remain present and unmodified

#### Scenario: Re-reifying one source leaves other sources' runs untouched

- **GIVEN** two sources, each with an active reification run and persisted mentions
- **WHEN** one source is re-reified
- **THEN** the other source's run remains active and its mentions remain unchanged

#### Scenario: A failed reification exposes no active run

- **GIVEN** a source whose reification fails partway through persisting its mentions
- **WHEN** the store is observed after the failure
- **THEN** no newly-active run exists for that source, the prior active run (if any) remains active, and no mentions from the failed attempt are readable

### Requirement: Mention identity is run-scoped with a stable cross-run occurrence key

A mention SHALL be identified by a surrogate id assigned at persistence and SHALL record an `anchor_kind` of source or revision.
Within a run, at most one source-anchored mention SHALL exist per `(chunk_id, source_chunk_span, surface_form)` and at most one revision-anchored mention per `(chunk_id, surface_form)`; persisting a mention whose class tuple already exists in that run SHALL NOT create a duplicate.
Each source-anchored mention SHALL also carry a `source_occurrence_key` that is a deterministic function of `(chunk_id, source_chunk_span, source_anchor_text)` and is therefore stable across runs for the same source occurrence; revision-anchored mentions SHALL be comparable across runs by `(chunk_id, surface_form)`.

Serves: replayable-duplicate-free-dataset

#### Scenario: The same occurrence in two runs gets distinct records but one occurrence key

- **GIVEN** the same source occurrence (same `chunk_id`, `source_chunk_span`, and anchor text) reified as a source-anchored mention under two different runs
- **WHEN** the two persisted mentions are compared
- **THEN** they are distinct mention records, each belonging to its own run, while their `source_occurrence_key`s are equal

#### Scenario: Re-running the same reification does not duplicate within a run

- **GIVEN** a chunk reified within a run
- **WHEN** the same chunk is reified again within that same run
- **THEN** no duplicate mention record is created

### Requirement: Every mention carries complete lexical provenance with declared span coordinates and no embedding

Every persisted mention SHALL carry, populated and non-null: `surface_form`, `chunk_id`, `anchor_kind` (source or revision), `input_kind` (raw or contextualized), `input_ref` (the input text the mention was read from), `input_span` (character offsets into that input text), and `blocking_keys`.
A source-anchored mention SHALL additionally carry a `source_chunk_span` locating one occurrence of its surface form in the raw chunk text; a revision-anchored mention SHALL carry no `source_chunk_span` — its raw provenance is the chunk itself plus its recorded input.
A mention read from raw input SHALL be source-anchored.
A mention SHALL NOT carry a stored context embedding; any embedding a consumer needs is recomputed on demand from the mention's recorded input text and `input_span` and is never persisted.

Serves: grounded-entity-graph, replayable-duplicate-free-dataset

#### Scenario: A persisted mention has all provenance fields populated and no embedding

- **GIVEN** any mention emitted by extraction and persisted
- **WHEN** the mention record is inspected
- **THEN** `surface_form`, `chunk_id`, `anchor_kind`, `input_kind`, `input_ref`, `input_span`, and `blocking_keys` are present and non-null, `source_chunk_span` is present iff the mention is source-anchored, and no context-embedding field is stored

#### Scenario: A source-anchored mention's span resolves to its surface form

- **GIVEN** a source-anchored mention
- **WHEN** its `source_chunk_span` is resolved against the raw chunk text
- **THEN** the resolved region equals the mention's `surface_form`

#### Scenario: A revision-resolved name absent from the raw chunk is persisted as revision-anchored

- **GIVEN** extraction reads a contextualized chunk whose revision resolves a reference into an entity name that does not occur in the raw chunk text
- **WHEN** mentions are persisted
- **THEN** that detection is persisted as a mention with `anchor_kind` revision, no `source_chunk_span`, and its input provenance populated

#### Scenario: A repeated surface form yields one source-anchored mention per occurrence

- **GIVEN** a detected surface form that occurs more than once in the raw chunk text
- **WHEN** mentions are persisted
- **THEN** one source-anchored mention exists per occurrence, each carrying the `source_chunk_span` of its own occurrence

### Requirement: Co-occurrence is resolvable without being stored on the mention row

For any mention, its intra-chunk co-occurrences SHALL be resolvable from the store.
A co-occurrence link SHALL be recorded at most once per unordered mention pair within a run; re-persisting a chunk's links SHALL NOT create duplicates.
Co-occurrence SHALL NOT be a required stored field on the mention row.

Serves: grounded-entity-graph, replayable-duplicate-free-dataset

#### Scenario: A mention's co-occurrences are resolvable

- **GIVEN** a chunk that produced two or more mentions in a run
- **WHEN** the co-occurrences of one of those mentions are queried
- **THEN** the other same-chunk mentions of that run are returned, without reading a co-occurrence field on the mention row

#### Scenario: Retrying a chunk's persistence does not duplicate co-occurrence links

- **GIVEN** a chunk whose mentions and co-occurrence links were persisted under a run
- **WHEN** the same chunk's mentions and links are persisted again within that run (a retry)
- **THEN** each unordered mention pair remains recorded exactly once

### Requirement: A mention's source chunk is resolvable

Every mention's `chunk_id` SHALL reference a persisted chunk, so a mention can always be traced back to the chunk it was extracted from.

Serves: grounded-entity-graph

#### Scenario: A mention resolves to an existing chunk

- **GIVEN** a persisted mention
- **WHEN** its `chunk_id` is looked up
- **THEN** the referenced chunk record exists in the store
