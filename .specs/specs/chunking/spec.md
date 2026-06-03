# Chunking Specification

> Synced from change `2026-05-18-chunking-splitter-foundation` on 2026-05-20

## Purpose

The chunking capability splits converted Markdown artifacts into ordered, structurally-faithful chunks for downstream embedding, retrieval, and knowledge-graph construction.
The splitter is a deterministic pure function that derives stable, content-addressable chunk identities, so a content edit and a structural move are independently observable downstream and re-processing is cheap and reproducible.

## Requirements

### Requirement: Splitter is a deterministic pure function

The splitter SHALL be a pure function of its inputs (`markdown_text`, `doc_id`, `converted_artifact_id`, `markdown_hash_xx64`) and the splitter's current behavior version (`splitter_version`).
Repeated invocations with identical inputs SHALL produce identical output: the same number of chunks in the same order, each chunk carrying identical values across every field (including `chunk_id`, `content_hash`, `heading_path`, `ordinal`, `text`, `char_count`, and all provenance fields).

The splitter SHALL NOT perform I/O of any kind — no database reads or writes, no network calls, no filesystem access, no subprocess invocation, no LLM calls.
The splitter SHALL NOT depend on per-process state (random seeds, wall-clock time, environment variables, process identity).

#### Scenario: Two invocations on the same input produce identical chunks

- **GIVEN** a Markdown artifact and its provenance context (`doc_id`, `converted_artifact_id`, `markdown_hash_xx64`)
- **WHEN** the splitter is invoked twice on those inputs within a single process
- **THEN** the two outputs are equal: same chunk count, same chunk ordering, and field-for-field equality on every chunk

#### Scenario: Invocations across separate processes produce identical chunks

- **GIVEN** a Markdown artifact and its provenance context, persisted to a deterministic form
- **WHEN** the splitter is invoked on those inputs in two independent processes built from the same `splitter_version`
- **THEN** the two outputs are equal field-for-field on every chunk, including `chunk_id` values

### Requirement: Chunk identity is derived from address and content

The `chunk_id` of every emitted chunk SHALL be a deterministic function of the chunk's address `(doc_id, heading_path, ordinal)` and its `content_hash`.
Two chunks whose address and `content_hash` are pairwise equal SHALL have equal `chunk_id`s; two chunks differing on either axis SHALL have different `chunk_id`s.

The `content_hash` SHALL be persisted as a separately observable field on every chunk so that consumers comparing two outputs can determine whether a differing `chunk_id` reflects an address change, a content change, or both.

#### Scenario: Same address and same content yield the same chunk_id

- **GIVEN** two splitter invocations producing chunks at the identical address `(doc_id, heading_path, ordinal)` with identical normalized text
- **WHEN** the resulting chunks are compared
- **THEN** their `chunk_id` values are equal and their `content_hash` values are equal

#### Scenario: Same address with different content yields a different chunk_id

- **GIVEN** two splitter invocations producing chunks at the identical address but with different normalized text
- **WHEN** the resulting chunks are compared
- **THEN** their `chunk_id` values differ and their `content_hash` values differ

#### Scenario: Different address with the same content yields a different chunk_id

- **GIVEN** two splitter invocations producing chunks at different addresses (for example, an unchanged section moved under a renamed heading) whose normalized text is identical
- **WHEN** the resulting chunks are compared
- **THEN** their `chunk_id` values differ even though their `content_hash` values are equal

### Requirement: Structural fidelity to the source artifact

Every body region of the source Markdown artifact SHALL be assigned to exactly one emitted chunk.
No emitted chunk's text SHALL span across a heading boundary; a chunk's `heading_path` SHALL identify the heading whose body the chunk belongs to.
The total ordering of chunks SHALL reflect the order of their body regions in the source artifact, such that traversing chunks by their `heading_path` and `ordinal` values reproduces the source's structural sequence.

#### Scenario: Source body regions are partitioned across chunks

- **GIVEN** a Markdown artifact with multiple body regions across multiple headings
- **WHEN** the splitter emits chunks
- **THEN** every body region of the source is present in exactly one chunk (no body region appears in two chunks, and no body region is absent from all chunks)

#### Scenario: No chunk spans a heading boundary

- **GIVEN** a Markdown artifact with two consecutive headings of equal or differing level, each followed by body content
- **WHEN** the splitter emits chunks
- **THEN** no emitted chunk contains text from both headings' bodies; each chunk's `heading_path` identifies exactly one heading whose body the chunk belongs to

#### Scenario: Chunk ordering reproduces source structural order

- **GIVEN** a Markdown artifact whose body regions appear in a specific order in the source
- **WHEN** the splitter emits chunks sorted by `heading_path` (in document-tree order) and `ordinal`
- **THEN** the resulting ordering matches the source order of the corresponding body regions

### Requirement: Size budget compliance with non-splittable block exception

Every chunk's `char_count` SHALL be at most the configured size budget, except when the chunk contains a single non-splittable block (fenced code block, table, list, blockquote, or math block) that itself exceeds the budget.
In the exception case, the chunk SHALL contain that non-splittable block in its entirety and `char_count` MAY exceed the budget.

A paragraph that exceeds the budget but cannot be divided without splitting an inline construct (link, image, inline code span, or inline math) SHALL be treated as non-splittable: it SHALL be emitted as one chunk that preserves the construct intact, and that chunk's `char_count` MAY exceed the budget.

No chunk SHALL contain a partial non-splittable block; non-splittable blocks SHALL appear in exactly one chunk in their entirety.

#### Scenario: Body within budget produces a single chunk

- **GIVEN** a heading whose body's character count is at or below the configured size budget
- **WHEN** the splitter processes the heading's body
- **THEN** the splitter emits one chunk for that body, whose `char_count` is at or below the size budget

#### Scenario: Splittable body over budget is paragraph-split within budget

- **GIVEN** a heading whose body's character count exceeds the size budget and consists of multiple paragraphs none of which individually exceeds the budget
- **WHEN** the splitter processes the heading's body
- **THEN** the splitter emits multiple chunks each with `char_count` at or below the size budget, and no paragraph is split across chunks

#### Scenario: Non-splittable block exceeding the budget is kept whole

- **GIVEN** a heading whose body contains a non-splittable block (e.g., a fenced code block) whose own character count exceeds the size budget
- **WHEN** the splitter processes the heading's body
- **THEN** the non-splittable block appears in exactly one chunk in its entirety, that chunk's `char_count` MAY exceed the size budget, and no other chunk contains any portion of that block

#### Scenario: Over-budget paragraph with an inline construct is kept whole

- **GIVEN** a heading whose body is a single paragraph that exceeds the size budget and contains an inline link, image, code span, or math span
- **WHEN** the splitter processes the heading's body
- **THEN** the splitter emits one chunk preserving the inline construct intact, that chunk's `char_count` MAY exceed the size budget, and the construct is not split across chunks

### Requirement: Defined behavior for heading-path edge cases

The splitter SHALL produce a defined `heading_path` for every chunk under each of the following source-structure cases.
`heading_path` values SHALL reflect only outer Markdown section boundaries; heading syntax appearing inside a non-section context SHALL NOT be treated as a section boundary.

The named cases the splitter SHALL handle are:

- **Pre-heading content** — body content appearing before the first heading.
- **Skipped heading levels** — a heading whose level is more than one deeper than its parent's level (e.g., `#` immediately followed by `###`).
- **Headings inside non-section contexts** — heading syntax appearing inside a fenced code block, list item, blockquote, or other non-section construct.
- **Empty heading bodies** — a heading with no body content between it and the next heading or end of document.
- **Heading-less documents** — a source artifact containing no headings.
- **Frontmatter** — YAML or TOML frontmatter at the start of the source artifact.

#### Scenario: Pre-heading content is chunked under the empty heading path

- **GIVEN** a Markdown artifact whose first non-empty body content appears before any heading
- **WHEN** the splitter processes the artifact
- **THEN** the pre-heading content is emitted as one or more chunks whose `heading_path` is empty, ordered before any chunks belonging to subsequent headings

#### Scenario: Skipped heading levels reflect actual nesting

- **GIVEN** a Markdown artifact containing a level-1 heading `A` followed directly by a level-3 heading `C` with body content under each
- **WHEN** the splitter processes the artifact
- **THEN** chunks belonging to `C`'s body carry a `heading_path` reflecting actual source nesting (`["A", "C"]`), with no inferred intermediate level

#### Scenario: Heading syntax inside a non-section context is not a section boundary

- **GIVEN** a Markdown artifact whose body under a heading contains a fenced code block, list item, or blockquote that itself contains text resembling Markdown heading syntax
- **WHEN** the splitter processes the artifact
- **THEN** no chunk's `heading_path` reflects the inner heading-like text as a section boundary; the enclosing heading's `heading_path` is preserved across the affected chunk(s)

#### Scenario: Empty heading body produces no chunk for that body

- **GIVEN** a Markdown artifact containing a heading with no body content between it and the next heading
- **WHEN** the splitter processes the artifact
- **THEN** no chunk is emitted for that empty body; the heading remains addressable through any descendant heading whose `heading_path` includes it

#### Scenario: Heading-less document produces chunks under the empty heading path

- **GIVEN** a Markdown artifact containing no headings
- **WHEN** the splitter processes the artifact
- **THEN** every emitted chunk carries `heading_path` equal to the empty path, and size-budget and paragraph-split policy apply identically to a normal body

#### Scenario: Frontmatter is preserved as a chunk

- **GIVEN** a Markdown artifact beginning with YAML or TOML frontmatter
- **WHEN** the splitter processes the artifact
- **THEN** the frontmatter is preserved as one or more chunks whose `heading_path` is empty and whose `ordinal` places them before all other root-level content; the splitter does not silently discard the frontmatter

### Requirement: Provenance and version stamping

Every emitted chunk SHALL carry the following provenance fields populated:

- `converted_artifact_id` — the caller-supplied identifier of the source artifact
- `markdown_hash_xx64` — the caller-supplied content-addressable hash of the source artifact
- `span` — a representation of the region of the source artifact from which the chunk was derived, sufficient to locate the chunk's text within the source
- `splitter_version` — the splitter's current behavior version

All chunks emitted from a single splitter invocation SHALL carry identical `converted_artifact_id`, `markdown_hash_xx64`, and `splitter_version` values, equal to the inputs and the splitter's current version constant respectively.

#### Scenario: Every chunk carries populated provenance fields

- **GIVEN** any splitter invocation that emits at least one chunk
- **WHEN** the emitted chunks are inspected
- **THEN** every chunk has non-null `converted_artifact_id`, `markdown_hash_xx64`, `span`, and `splitter_version` values

#### Scenario: Provenance is uniform across one invocation

- **GIVEN** a splitter invocation that emits multiple chunks
- **WHEN** the chunks' `converted_artifact_id`, `markdown_hash_xx64`, and `splitter_version` values are compared
- **THEN** these three fields are equal across all chunks emitted in that invocation, and equal to the input values and the splitter's current version constant

#### Scenario: Span locates the chunk within the source

- **GIVEN** an emitted chunk and its source artifact
- **WHEN** the chunk's `span` is resolved against the source
- **THEN** the resolved region of the source contains the text from which the chunk's normalized `text` was derived

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

## Technical Notes

- **Implementation**: `src/aizk/chunking/` — `split()` and `Chunk` in `splitter.py` / `datamodel.py`; `SPLITTER_VERSION` and `DEFAULT_SIZE_BUDGET` in `_version.py`
- **Dependencies**: conversion-worker (consumes its normalized Markdown artifacts and `markdown_hash_xx64`); shared markdown-hash helper `aizk.utilities.hashing.compute_markdown_hash` (reused by both stages)
- **Design record**: decisions (parser choice, hash function, canonical `chunk_id` serialization, `content_hash` normalization, span representation, size budget, `heading_path`/`ordinal` semantics, sentence fallback, `splitter_version`) live in `docs/decision-record/005-chunking.md`
- **Calibration**: `DEFAULT_SIZE_BUDGET = 4096` chars, calibrated against the benchmark corpus; see `data/chunking-calibration/findings.md`
- **Version discipline**: `splitter_version` is a monotonic integer bumped on any change to observable output; a fixture-suite snapshot test fails closed if output drifts without a bump
