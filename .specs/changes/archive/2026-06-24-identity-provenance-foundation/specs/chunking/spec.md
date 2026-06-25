# Delta for chunking

## RENAMED Requirements

- FROM: `### Requirement: Chunk identity is derived from address and content`

- TO: `### Requirement: Chunk identity is a stable surrogate reused by a sameness-key`

- FROM: `### Requirement: Chunk identities are immutable and content-addressed`

- TO: `### Requirement: Chunk identities are immutable stable surrogates`

## MODIFIED Requirements

### Requirement: Splitter is a deterministic pure function

The splitter SHALL be a pure function of its inputs (`markdown_text`, `source_id`, `converted_artifact_id`, `markdown_hash_xx64`) and the splitter's current behavior version (`splitter_version`).
Repeated invocations with identical inputs SHALL produce identical output: the same number of chunks in the same order, each chunk carrying identical values across every field it produces (including `content_hash`, `heading_path`, `ordinal`, `text`, `char_count`, and all provenance fields).
The splitter does not assign `chunk_id`; chunk identity is a stable surrogate assigned at persistence (see «Chunk identity is a stable surrogate reused by a sameness-key»).

The splitter SHALL NOT perform I/O of any kind — no database reads or writes, no network calls, no filesystem access, no subprocess invocation, no LLM calls.
The splitter SHALL NOT depend on per-process state (random seeds, wall-clock time, environment variables, process identity).

Serves: coherent-pipeline-foundation, portable-knowledge

> Previously: the splitter input was named `doc_id`, and the splitter's deterministic output included a content-addressed `chunk_id`. Chunk identity is now a surrogate assigned at persistence, so `chunk_id` is no longer a splitter output; determinism applies to the fields the splitter produces (notably `content_hash`).

#### Scenario: Two invocations on the same input produce identical chunks

- **GIVEN** a Markdown artifact and its provenance context (`source_id`, `converted_artifact_id`, `markdown_hash_xx64`)
- **WHEN** the splitter is invoked twice on those inputs within a single process
- **THEN** the two outputs are equal: same chunk count, same chunk ordering, and field-for-field equality on every produced field of every chunk

#### Scenario: Invocations across separate processes produce identical chunks

- **GIVEN** a Markdown artifact and its provenance context, persisted to a deterministic form
- **WHEN** the splitter is invoked on those inputs in two independent processes built from the same `splitter_version`
- **THEN** the two outputs are equal field-for-field on every produced field, including `content_hash` values

### Requirement: Chunk identity is a stable surrogate reused by a sameness-key

A chunk's `chunk_id` SHALL be a stable surrogate identity: assigned once at persistence, never recomputed, not derived from the chunk's content, and not embedding any database-local identifier.
A chunk's sameness-key SHALL be `(source_id, heading_path, ordinal, content_hash)`; persistence SHALL reuse the existing `chunk_id` for a chunk whose sameness-key is already present and SHALL assign a new `chunk_id` for one whose sameness-key is not present.
The `content_hash` SHALL be persisted as a separately observable field on every chunk so that consumers comparing two chunks can determine whether a difference reflects an address change, a content change, or both.

Serves: portable-knowledge, coherent-pipeline-foundation

> Previously: `chunk_id` was a content-addressed deterministic function of the chunk's address `(doc_id, heading_path, ordinal)` and its `content_hash`. Identity is now a surrogate whose cross-generation reuse is keyed on the sameness-key `(source_id, heading_path, ordinal, content_hash)`, preserving the same observable identity behavior.

#### Scenario: Same address and same content yield the same chunk_id

- **GIVEN** two persisted chunks at the identical address `(source_id, heading_path, ordinal)` with identical normalized text
- **WHEN** the resulting chunks are compared
- **THEN** their `chunk_id` values are equal and their `content_hash` values are equal

#### Scenario: Same address with different content yields a different chunk_id

- **GIVEN** two persisted chunks at the identical address but with different normalized text
- **WHEN** the resulting chunks are compared
- **THEN** their `chunk_id` values differ and their `content_hash` values differ

#### Scenario: Different address with the same content yields a different chunk_id

- **GIVEN** two persisted chunks at different addresses (for example, an unchanged section moved under a renamed heading) whose normalized text is identical
- **WHEN** the resulting chunks are compared
- **THEN** their `chunk_id` values differ even though their `content_hash` values are equal

### Requirement: Chunk identities are immutable stable surrogates

A persisted chunk's stable identity facts SHALL NOT be modified or deleted by ordinary processing; `chunk_id` SHALL be a stable surrogate (not content-derived, not embedding any database-local identifier) and SHALL NOT be scoped to the generation that produced it.
The chunk identity SHALL carry only stable facts; generation-varying facts SHALL live on the emitting generation, so a chunk emitted unmodified by more than one generation keeps a single, unchanged identity shared across them.
Persisting a chunk whose sameness-key already exists SHALL reuse the existing identity rather than create a duplicate or modify it; a chunk whose sameness-key is not present SHALL be stored as exactly one new identity.

Serves: portable-knowledge, idempotent-duplicate-free-pipeline

> Previously: `chunk_id` was content-addressed, and reuse-versus-create was decided by whether that content-addressed `chunk_id` already existed. It is now a surrogate, and reuse is decided by the sameness-key `(source_id, heading_path, ordinal, content_hash)`.

#### Scenario: Re-persisting an existing chunk reuses the identity

- **GIVEN** a chunk already persisted with a given sameness-key
- **WHEN** a chunk with the same sameness-key is persisted again
- **THEN** no duplicate identity is created and the existing identity is unmodified

#### Scenario: A novel chunk is stored once

- **GIVEN** a chunk whose sameness-key is not present in the store
- **WHEN** that chunk is persisted
- **THEN** exactly one new chunk identity is created carrying that chunk's stable facts

### Requirement: Emitted chunks are persisted with complete fidelity

Every chunk emitted by the splitter for a document SHALL be persisted to a durable store.
The splitter emits `content_hash`, `heading_path`, `ordinal`, `text`, `char_count`, `source_id`, `markdown_hash_xx64`, `span`, and `splitter_version`; it does not emit `chunk_id`, which persistence assigns as a stable surrogate (reused or newly minted per the chunk's sameness-key).
Each persisted chunk SHALL be recoverable with every emitted field equal to what the splitter emitted, plus its persistence-assigned `chunk_id`.
Persistence SHALL NOT alter, normalize, truncate, or drop any field.
Stable identity facts (the assigned `chunk_id`, `content_hash`, `heading_path`, `ordinal`, `text`, `char_count`, `source_id`) SHALL be recorded on the stable chunk identity, while facts that vary by chunking generation — the source `markdown_hash_xx64`, the `splitter_version`, and the chunk's `span` in that generation's markdown — SHALL be recorded against the generation that emitted the chunk, not as mutable facts on the shared identity.
Recovering an emitted chunk MAY therefore require joining its identity to the generation that emitted it.

Serves: coherent-pipeline-foundation

> Previously: identity facts were recorded on a "content-addressed chunk identity" and the source identity was unnamed in the field list; the identity is now a stable surrogate and the source identity is named `source_id`.

#### Scenario: A persisted chunk round-trips field-for-field

- **GIVEN** a splitter invocation that emits a set of chunks for a document
- **WHEN** those chunks are persisted and then reconstructed from their identity and the emitting generation
- **THEN** each reconstructed chunk equals its emitted chunk on every emitted field and additionally carries its persistence-assigned `chunk_id`, with no field altered, normalized, or missing

#### Scenario: The full emitted set is present in the generation

- **GIVEN** a splitter invocation that emits N chunks for a document
- **WHEN** the chunks are persisted under a chunking generation
- **THEN** all N chunks are recoverable from that generation's manifest, with none dropped and none added

#### Scenario: A chunk's span is recorded per generation, not on the shared identity

- **GIVEN** a chunk that is byte-identical across two chunking generations of the same source but sits at a different offset because preceding content changed length
- **WHEN** both generations are persisted
- **THEN** the chunk keeps a single stable identity, and each generation's manifest records that chunk's own `span` for that generation

### Requirement: Chunking generations are source-scoped, record what they consumed and produced, and supersede at the generation level

Each persisted chunk SHALL be associated, through an append-only manifest entry, with the chunking generation that produced it; that manifest entry SHALL capture the chunk's `span` in the generation's source markdown.
A source SHALL have at most one active chunking generation at a time, scoped by its **durable source identity (`source_id`)** — not by a per-conversion artifact id — so that re-conversion of the same source supersedes within one scope rather than forking a parallel current generation.
Each chunking generation SHALL record what it consumed: a locator to the exact source markdown it read (so the input is retrievable) and that markdown's hash (so the input is verifiable).
Re-chunking a source — after a `splitter_version` change or a re-conversion that changes its content — SHALL create a new chunking generation and SHALL NOT mutate or delete any prior chunk identity or prior manifest entry.
Superseding the prior generation SHALL be expressed only as a transition of its status from active to superseded; a `chunk_id` SHALL be current if and only if it is in the source's active generation's manifest.

Serves: coherent-pipeline-foundation, idempotent-duplicate-free-pipeline

> Previously: the durable source identity scoping a generation was named `aizk_uuid`.

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
