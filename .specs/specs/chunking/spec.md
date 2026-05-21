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

## Technical Notes

- **Implementation**: `src/aizk/chunking/` — `split()` and `Chunk` in `splitter.py` / `datamodel.py`; `SPLITTER_VERSION` and `DEFAULT_SIZE_BUDGET` in `_version.py`
- **Dependencies**: conversion-worker (consumes its normalized Markdown artifacts and `markdown_hash_xx64`); shared markdown-hash helper `aizk.utilities.hashing.compute_markdown_hash` (reused by both stages)
- **Design record**: decisions (parser choice, hash function, canonical `chunk_id` serialization, `content_hash` normalization, span representation, size budget, `heading_path`/`ordinal` semantics, sentence fallback, `splitter_version`) live in `docs/decision-record/005-chunking.md`
- **Calibration**: `DEFAULT_SIZE_BUDGET = 4096` chars, calibrated against the benchmark corpus; see `data/chunking-calibration/findings.md`
- **Version discipline**: `splitter_version` is a monotonic integer bumped on any change to observable output; a fixture-suite snapshot test fails closed if output drifts without a bump
