# Delta for chunking

## ADDED Requirements

### Requirement: Emitted chunks are persisted with complete fidelity

Every chunk emitted by the splitter for a document SHALL be persisted to a durable store, and each emitted chunk SHALL be recoverable with every field equal to the emitted chunk — `chunk_id`, `content_hash`, `heading_path`, `ordinal`, `text`, `char_count`, the source identity, `markdown_hash_xx64`, `span`, and `splitter_version`.
Persistence SHALL NOT alter, normalize, truncate, or drop any chunk field.
Stable identity facts (`chunk_id`, `content_hash`, `heading_path`, `ordinal`, `text`, `char_count`, source identity) SHALL be recorded on the content-addressed chunk identity, while facts that vary by chunking generation — the source `markdown_hash_xx64`, the `splitter_version`, and the chunk's `span` in that generation's markdown — SHALL be recorded against the generation that emitted the chunk, not as mutable facts on the shared identity.
Recovering an emitted chunk MAY therefore require joining its identity to the generation that emitted it.

#### Scenario: A persisted chunk round-trips field-for-field

- **GIVEN** a splitter invocation that emits a set of chunks for a document
- **WHEN** those chunks are persisted and then reconstructed from their identity and the emitting generation
- **THEN** each reconstructed chunk equals its emitted chunk on every field, with no field altered, normalized, or missing

#### Scenario: The full emitted set is present in the generation

- **GIVEN** a splitter invocation that emits N chunks for a document
- **WHEN** the chunks are persisted under a chunking generation
- **THEN** all N chunks are recoverable from that generation's manifest, with none dropped and none added

#### Scenario: A chunk's span is recorded per generation, not on the shared identity

- **GIVEN** a chunk that is byte-identical across two chunking generations of the same source but sits at a different offset because preceding content changed length
- **WHEN** both generations are persisted
- **THEN** the chunk keeps a single content-addressed identity, and each generation's manifest records that chunk's own `span` for that generation

### Requirement: Chunk identities are immutable and content-addressed

A persisted chunk's stable identity facts SHALL NOT be modified or deleted by ordinary processing; `chunk_id` SHALL remain content-addressed (a function of the chunk's address and `content_hash`) and SHALL NOT be scoped to the generation that produced it.
The chunk identity SHALL carry only stable facts; generation-varying facts SHALL live on the emitting generation, so a chunk emitted unmodified by more than one generation keeps a single, unchanged identity shared across them.
Persisting a chunk whose `chunk_id` already exists SHALL reuse the existing identity rather than create a duplicate or modify it; a chunk with a `chunk_id` not present SHALL be stored as exactly one new identity.

#### Scenario: Re-persisting an existing chunk reuses the identity

- **GIVEN** a chunk already persisted with a given `chunk_id`
- **WHEN** a chunk with the same `chunk_id` is persisted again
- **THEN** no duplicate identity is created and the existing identity is unmodified

#### Scenario: A novel chunk is stored once

- **GIVEN** a `chunk_id` not present in the store
- **WHEN** that chunk is persisted
- **THEN** exactly one new chunk identity is created carrying that chunk's stable facts

### Requirement: Chunking generations are source-scoped, record what they consumed and produced, and supersede at the generation level

Each persisted chunk SHALL be associated, through an append-only manifest entry, with the chunking generation that produced it; that manifest entry SHALL capture the chunk's `span` in the generation's source markdown.
A source SHALL have at most one active chunking generation at a time, scoped by its **durable source identity (`aizk_uuid`)** — not by a per-conversion artifact id — so that re-conversion of the same source supersedes within one scope rather than forking a parallel current generation.
Each chunking generation SHALL record what it consumed: a locator to the exact source markdown it read (so the input is retrievable) and that markdown's hash (so the input is verifiable).
Re-chunking a source — after a `splitter_version` change or a re-conversion that changes its content — SHALL create a new chunking generation and SHALL NOT mutate or delete any prior chunk identity or prior manifest entry.
Superseding the prior generation SHALL be expressed only as a transition of its status from active to superseded; a `chunk_id` SHALL be current if and only if it is in the source's active generation's manifest.

#### Scenario: A chunk shared across generations stays current without identity mutation

- **GIVEN** a source whose chunks are persisted under generation A, then re-chunked into generation B such that some `chunk_id`s appear in both
- **WHEN** generation B becomes active and generation A is marked superseded
- **THEN** each shared `chunk_id`'s identity is unchanged and is current by its manifest entry in generation B, with no identity mutated

#### Scenario: A chunk only in the prior generation becomes non-current via generation status

- **GIVEN** a source re-chunked into generation B such that some prior `chunk_id`s are not emitted by generation B
- **WHEN** generation B becomes active and generation A is marked superseded
- **THEN** each such `chunk_id` is no longer current (its only generation is superseded) and its identity remains present and unmodified

#### Scenario: A chunk only in the new generation is current via its manifest entry

- **GIVEN** a source re-chunked into generation B such that some `chunk_id`s appear only in generation B
- **WHEN** generation B becomes active
- **THEN** each such `chunk_id` is current by its manifest entry in generation B, and no prior identity or manifest entry is mutated or removed

#### Scenario: A superseded generation's consumed input and manifest remain recoverable

- **GIVEN** a source re-chunked from generation A to generation B
- **WHEN** generation A's consumed markdown and produced chunk set are inspected after supersession
- **THEN** A's recorded input locator, input hash, and manifest are present and unmodified, so what A consumed and produced is recoverable without re-running the splitter
