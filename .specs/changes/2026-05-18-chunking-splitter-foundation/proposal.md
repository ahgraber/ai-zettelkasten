# Proposal: Chunking Splitter Foundation

## Intent

The conversion stage produces Markdown artifacts from source documents but no downstream stage consumes them yet.
Embedding, retrieval, knowledge-graph construction, and any future LLM enrichment all need short, addressable chunks rather than whole documents.

This change establishes **document-structure chunking** as the chunking strategy: split a converted Markdown artifact along its heading hierarchy, with paragraph-level fallback within a heading when bodies exceed the size budget, no LLM calls, no overlap.
It introduces the foundational piece of that strategy — the splitter function and its associated chunk data model — as a standalone, deterministic, pure unit, with no persistence and no pipeline integration.
A subsequent change wires the splitter into an event-sourced chunking pipeline that consumes `ConversionOutput` and persists chunks to the database.

## Scope

**In scope:**

- A pure splitter function exposed by the `aizk.chunking` package that, given a converted Markdown artifact plus provenance context (`doc_id`, `converted_artifact_id`, `markdown_hash_xx64`), returns an ordered list of structural chunk records.
- A typed chunk data model (pydantic / dataclass) carrying: `chunk_id`, `content_hash`, `doc_id`, `heading_path`, `ordinal`, `text`, `char_count`, plus provenance fields (`converted_artifact_id`, `markdown_hash_xx64`, `span`, `splitter_version`).
- Deterministic identity derivation: `chunk_id` derived from `(doc_id, heading_path, ordinal, content_hash)` via a stable hash function.
- A `splitter_version` constant captured on every emitted chunk, conventionally bumped on any behavior-affecting change.
- Defined behavior for the splitter's input-domain edge cases: pre-heading content, skipped heading levels, non-paragraph blocks (code, table, list, blockquote, math), headings nested in non-section contexts, empty / heading-less documents, frontmatter handling.
- Configuration surface for size budget (in characters) and paragraph-split policy, captured by `splitter_version`.
- Unit tests covering every requirement, with partition coverage for universal claims.

**Out of scope:**

- Event-sourced chunking pipeline (worker, `record_transition`, event log) — next change.
- Database schema for chunks and Alembic migrations — next change.
- Reading `ConversionOutput` rows, S3 fetches, or any DB / network I/O — next change.
- API surface for chunks — no chunking HTTP surface in this project today.
- Contextualization (Anthropic-style enrichment), coreference resolution, knowledge-graph construction.
- Decisions about which downstream stages consume chunks and how they signal invalidation — out of this change.
- Any LLM calls.

## Approach

The splitter lives in a new `aizk.chunking` package as a pure function (or small module) taking the converted Markdown text plus the provenance context fields and returning an ordered list of chunks.
The implementation walks the Markdown heading tree to identify section boundaries, emits one chunk per heading body when within budget, and falls back to paragraph splitting within over-budget bodies.
Non-paragraph blocks (fenced code, tables, blockquotes, lists, math blocks) are detected and never split mid-block.
Headings nested inside such blocks are not treated as section boundaries.

`chunk_id` is the hex digest of a hash over a canonical serialization of `(doc_id, heading_path, ordinal, content_hash)`.
The specific hash function (likely xxh64 to match the conversion stage's `markdown_hash_xx64`, alternatively BLAKE2b) and the canonical serialization format live in `design.md`.

`content_hash` is computed over the chunk's normalized text body.
It is persisted alongside `chunk_id` so consumers can tell which axis — address or content — moved when a `chunk_id` differs from a prior run.

`splitter_version` is a string constant in the splitter module.
It captures all splitter configuration (size budget, paragraph-split policy, edge-case rules, normalization) and is conventionally bumped on any behavior-affecting change.
Pure refactors with no observable output change do not bump it.

Size budget is expressed in characters rather than tokens, keeping the splitter independent of any specific tokenizer.
Across major tokenizers (cl100k, llama, anthropic) a ~4 chars/token ratio for English holds within ~25%, which is adequate for chunk-size policy.
Default budget value and the exact handling of edge cases (e.g., a single paragraph that itself exceeds the budget) live in `design.md`.

This change does not yet decide whether to use [chonkie](https://github.com/chonkie-inc/chonkie) as an internal primitive for paragraph splitting or character counting.
The splitter's public contract is independent of that choice; the decision can be made during implementation without spec churn.
LangChain and LlamaIndex are excluded as dependency options to avoid their transitive surface area and framework gravity.

## Schema Impact

**Database (SQLite):** No changes.
This change introduces no migrations and no new tables.
Persistence of chunks is the next change's concern.

**OpenAPI:** No changes.
This change introduces no API endpoints.
The pre-change snapshot in `.specs/schemas/conversion-api-openapi.json` is the expected post-change snapshot; no `schemas/expected.md` is generated for this change.

## Decisions Carried Into Design

- The splitter is a **pure function**, not a class hierarchy or framework.
  Any extension points are exposed via configuration captured by `splitter_version`, not via inheritance or plugin protocols.
- `splitter_version` is a single string identifier, not a structured config object.
  Configuration values that affect behavior are encoded into the version string by convention; the spec contract is "same version → same output," not "expose a config schema."
- `chunk_id` derivation MUST be cross-process stable: given the same canonical inputs, two independent processes (current and future) produce the same id.
  This forbids hash functions seeded from per-process state.

## Open Questions

- **`doc_id` identifier semantics.**
  Is `doc_id` the source's persistent identity (`aizk_uuid` from `ConversionOutput`), the conversion artifact's row id (`ConversionOutput.id`), or a separate identifier introduced by the chunking stage?
  Recommendation: `aizk_uuid`, since it persists across re-conversion of the same source, but the choice belongs to the pipeline change and may motivate revising the chunk data model.
  For this change, the splitter accepts `doc_id` as an opaque identifier supplied by the caller — its semantics are deferred to the pipeline change.
- **Markdown parser.**
  Which Markdown parser does the splitter use to identify headings, paragraphs, and non-paragraph blocks?
  Candidates: `markdown-it-py`, `mistletoe`, `cmarkgfm`, chonkie-internal handling, or a hand-rolled walker.
  Design-level decision; affects test fixtures and edge-case coverage.
