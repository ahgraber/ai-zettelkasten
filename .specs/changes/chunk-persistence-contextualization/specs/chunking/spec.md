# Delta for chunking

## ADDED Requirements

### Requirement: Emitted chunks are persisted with complete fidelity

Every chunk emitted by the splitter for a document SHALL be persisted to a durable store, and each persisted chunk SHALL be retrievable with every field equal to the emitted chunk — `chunk_id`, `content_hash`, `heading_path`, `ordinal`, `text`, `char_count`, `converted_artifact_id`, `markdown_hash_xx64`, `span`, and `splitter_version`.
Persistence SHALL NOT alter, normalize, truncate, or drop any chunk field.

#### Scenario: A persisted chunk round-trips field-for-field

- **GIVEN** a splitter invocation that emits a set of chunks for a document
- **WHEN** those chunks are persisted and then read back from the store
- **THEN** each read-back chunk equals its emitted chunk on every field, with no field altered, normalized, or missing

#### Scenario: The full emitted set is present in the run

- **GIVEN** a splitter invocation that emits N chunks for a document
- **WHEN** the chunks are persisted under a chunking run
- **THEN** all N chunks are retrievable as members of that run, with none dropped and none added

### Requirement: Chunk content rows are immutable and content-addressed

A persisted chunk's content fields SHALL NOT be modified or deleted by ordinary processing; `chunk_id` SHALL remain content-addressed (a function of the chunk's address and `content_hash`) and SHALL NOT be scoped to the run that produced it.
Persisting a chunk whose `chunk_id` already exists SHALL reuse the existing row rather than create a duplicate or modify it; a chunk with a `chunk_id` not present SHALL be stored as exactly one new row.

#### Scenario: Re-persisting an existing chunk reuses the row

- **GIVEN** a chunk already persisted with a given `chunk_id`
- **WHEN** a chunk with the same `chunk_id` is persisted again
- **THEN** no duplicate row is created and the existing row is unmodified

#### Scenario: A novel chunk is stored once

- **GIVEN** a `chunk_id` not present in the store
- **WHEN** that chunk is persisted
- **THEN** exactly one new row is created carrying that chunk's fields

### Requirement: Chunks belong to chunking runs, and re-chunking supersedes at the run level

Each persisted chunk SHALL be associated, through append-only run membership, with the chunking run that produced it; a converted artifact SHALL have at most one active chunking run at a time.
Re-chunking a converted artifact — after a `splitter_version` change or a re-conversion that changes its content — SHALL create a new chunking run and SHALL NOT mutate or delete any prior chunk row or prior membership.
Superseding the prior run SHALL be expressed only as a transition of run status from active to superseded; a `chunk_id` SHALL be current if and only if it is a member of the document's active run.

#### Scenario: A chunk shared across runs stays current without row mutation

- **GIVEN** a converted artifact whose chunks are persisted under run A, then re-chunked into run B such that some `chunk_id`s appear in both runs
- **WHEN** run B becomes active and run A is marked superseded
- **THEN** each shared `chunk_id`'s row is unchanged and is current by membership in run B, with no row mutated

#### Scenario: A chunk only in the prior run becomes non-current via run status

- **GIVEN** a converted artifact re-chunked into run B such that some prior `chunk_id`s do not appear in run B
- **WHEN** run B becomes active and run A is marked superseded
- **THEN** each such `chunk_id` is no longer current (its only run is superseded) and its row remains present and unmodified

#### Scenario: A chunk only in the new run is current via membership

- **GIVEN** a converted artifact re-chunked into run B such that some `chunk_id`s appear only in run B
- **WHEN** run B becomes active
- **THEN** each such `chunk_id` is current by membership in run B, and no prior row or membership is mutated or removed
