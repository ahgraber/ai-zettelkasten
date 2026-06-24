# 005 - Chunking

## Status

- 22 June 2025 — Proposed
- 18 May 2026 — Revised
- 18 May 2026 — Amended (docling-native input alternatives evaluated)
- 23 June 2026 — Partly superseded by the `identity-provenance-foundation` change: chunk identity is now a stable surrogate assigned at persistence (no longer content-addressed), and `doc_id` is renamed `source_id`.

## Context

Downstream stages (embedding, retrieval, knowledge-graph construction) consume short, addressable units of a document rather than the whole document.
Chunking is the stage that produces those units from a converted document.

A Zettelkasten favors _atomicity_: each unit should represent one coherent idea so it can be linked, retrieved, and recombined.
Chunks are also the substrate for later LLM enrichment (e.g., contextualization, coreference resolution) and graph construction (entity nodes, relation edges) — those downstream stages reference chunks by identity, so chunk identity, ordering, and provenance must be stable and reproducible.

This ADR was originally proposed with an LLM-extraction approach inspired by Claimify (extract atomic claims; each claim is a chunk).
An ingest experiment using that approach cost ~$30 to process 5 documents (≈$6/doc), which does not scale to a personal Zettelkasten corpus.
Beyond cost, extraction-as-chunking erodes provenance (chunks are LLM rewrites, not source text), is hard to re-run when models or strategies change, and handles narrative/theoretical text poorly.
We are revising the decision to reflect that.

## Decision

### Selected Approach

Chunking produces **document-structure chunks**: contiguous slices of the source document derived from its heading hierarchy, with paragraph-level splitting applied only within a heading when a heading's body exceeds the size budget.
No LLM calls are made in the splitting itself; chunking is a deterministic, pure function of the converted document.

Key properties:

- **Heading-aware splitting.**
  The Markdown heading tree (`H1 → H2 → H3 → …`) defines the primary boundaries.
  A chunk never crosses a heading boundary or merges sibling headings.

- **Paragraph fallback within a heading.**
  If a heading's body exceeds the size budget, the body is split on paragraph boundaries.
  No overlap between paragraph chunks.

- **No cross-heading merging or overlap.**
  Atomicity is preferred over RAG-style overlap; downstream enrichment can supply missing context if needed.

- **Deterministic identity.**

  > Superseded by the `identity-provenance-foundation` change (June 2026): `chunk_id` is now a stable surrogate assigned at persistence and reused across generations by the sameness-key `(source_id, heading_path, ordinal, content_hash)`; `doc_id` is renamed `source_id`.
  The content-addressed scheme described below is the original decision, retained as history — the same observable behavior (a content edit and a structural move are independently observable) now rests on the `content_hash` column and the sameness-key, not on the identity itself.

  Each chunk has a `chunk_id` deterministically derived from **both** its address `(doc_id, heading_path, ordinal)` and its `content_hash`.
  Chunking is a pure function of the converted artifact: same input + same `splitter_version` → same `chunk_id`s.
  Any change — address or content — yields a new `chunk_id`.
  Old `chunk_id`s that no longer appear in the output are the unambiguous signal that downstream artifacts (embeddings, contextualizations, graph nodes/edges) tied to them are stale and must be invalidated/cleaned up.

- **Document-level change gate.**
  Re-chunking is skipped entirely when the conversion stage's `markdown_hash_xx64` is unchanged for a document AND `splitter_version` is unchanged.
  This makes re-runs cheap and idempotent in the common case (re-conversion produced the same Markdown).

- **Reconstruction metadata.**
  `heading_path` and `ordinal` are first-class fields on every chunk.
  The full document can be reconstructed in order from chunk metadata alone.

- **Event-sourced state transitions.**
  The chunking pipeline mirrors the conversion stage: state transitions are recorded through a single write path (e.g., `record_transition`) and persisted as an append-only event log, so chunking is replayable, observable, and resumable.

### Rationale

- **Cost.**
  Splitting is free.
  LLM enrichment is opt-in and deferred to later stages, so the chunking stage itself never blocks ingest on cost.
- **Provenance.**
  Chunks are exact source bytes (modulo normalization), so a chunk can always be cited back to a span in the source document.
- **Re-runnability.**
  Chunking is a deterministic function of `(converted artifact, splitter_version)`.
  When `markdown_hash_xx64` and `splitter_version` are both unchanged, chunking is a no-op (idempotent skip).
  When either changes, chunks are re-emitted; `chunk_id` churn directly identifies which chunks (and which downstream artifacts) are stale.
- **Cheap structural graph.**
  The heading hierarchy is _itself_ a graph (doc → section → subsection → paragraph).
  We do not materialize structural edges in this stage — `heading_path` and `ordinal` make those relationships latent in chunk metadata, materializable on demand by the future graph stage.

### Consequences

#### Positive Impacts

- Ingest cost for chunking is near zero; corpus size is no longer a budgeting concern at this stage.
- Chunks are stable, hashable artifacts that downstream stages can safely reference.
- LLM enrichment, coreference resolution, and graph construction can each evolve independently without invalidating chunk identity.
- Document reconstruction (e.g., for display, audit, or re-export) is always possible from chunk metadata.

#### Potential Risks

- Structural chunks under deep headings may rely on framing introduced higher in the heading tree, or contain unresolved pronouns referencing surrounding text.
  This is a retrieval-quality consideration, not a correctness issue — atomicity holds at the structural level; downstream contextualization can enrich where helpful.
- The size budget and heading-handling policy are tuning knobs; poor defaults can produce chunks that are too small (sparse signal) or too large (poor retrieval precision).
- Documents with shallow or absent heading structure (e.g., a long-form essay with no headings) degrade to paragraph-only splitting, which may not yield strong atomic units.

#### Mitigation Strategies

- **Self-containment** of the heading-and-paragraph atom is the structural target; contextualization (see Future Work) is _enrichment_ for cases where broader framing improves retrieval, not a prerequisite for atomicity.
- **Tuning** is exposed via configuration; defaults are chosen for short, dense documents typical of a Zettelkasten and revisited as the corpus grows.
- **Heading-poor documents** still benefit from paragraph splitting; if quality is poor in practice, the contextualization stage can compensate, or a per-document strategy override can be introduced later.

### Alternative Considered

#### Option 1: Extraction-as-chunking (Claimify-style)

The original proposal: use an LLM pipeline (selection → disambiguation → decomposition) to extract atomic claims; each claim is a chunk.

- _Pros._
  Chunks are intrinsically atomic and self-contained; aligns closely with the Zettelkasten ideal.
- _Cons._
  ~$6/document in the ingest experiment; provenance is rewritten text rather than source bytes; Claimify is question-anchored (poor fit for documents without a Q–A frame); handles narrative/theoretical text poorly; expensive to re-run when models or policies change.
- _Reason for not selecting._
  Cost is the dominant blocker for a personal corpus.
  The atomicity benefit can be approximated downstream via contextualization without rewriting source text.

#### Option 2: Chonkie built-in chunkers

[chonkie](https://github.com/chonkie-inc/chonkie) provides token, sentence, recursive, semantic, and code chunkers.

- _Pros._
  Mature library; useful primitives for token counting and recursive splitting.
- _Cons._
  No chunker that walks a Markdown heading tree and treats heading boundaries as hard cuts in the way this ADR requires.
  Recursive chunking with Markdown separators approximates the behavior but does not preserve `heading_path` metadata or guarantee no-cross-heading-merging.
- _Reason for not selecting (as the structural splitter)._
  We implement the heading-tree splitter ourselves to keep the contract explicit.
  Chonkie may still be used as an internal primitive for paragraph-level fallback within a heading.

#### Option 3: LangChain `MarkdownHeaderTextSplitter` / LlamaIndex equivalents

Both frameworks expose a heading-aware Markdown splitter close to what this ADR specifies.

- _Pros._
  Drop-in implementation of the desired behavior.
- _Cons._
  Pulling in LangChain or LlamaIndex as a dependency for one splitter is a heavy commitment with significant transitive surface area, and risks pulling the project toward each framework's broader abstractions.
- _Reason for not selecting._
  Avoid the dependency; the heading-tree splitter is small enough to own.

#### Option 4: DoclingDocument as splitter input

The conversion stage parses to `DoclingDocument` then serializes to Markdown and discards the structured doc.
The splitter could instead consume the live `DoclingDocument` (in-process), its persisted `doc.json` (separate worker), or both alongside Markdown.

- _Pros._
  Eliminates the in-repo Markdown parser dependency and roughly half the heading-edge-case scenarios in the chunking spec (frontmatter, headings inside non-section contexts, and skipped heading levels are resolved upstream by docling).
  Removes the Markdown-flavor-drift risk between docling and `markdown-it-py`.
- _Cons._
  **Storage.**
  `doc.json` is 5–500× the Markdown size — the conversion stage runs with `generate_page_images=True` and `images_scale=2`, embedding base64 page-image PNGs inline.
  **Schema drift.**
  `DoclingDocument` is an evolving pydantic model with no published JSON-schema version; docling-core has shipped ~one minor release every 5–6 days since Oct 2024, with item-model changes inside minors.
  Persisted `doc.json` files are not guaranteed to deserialize under future docling-core versions.
  **`content_hash` churn.**
  The Markdown serializer injects HTML comments and HTML-table blocks that have no equivalent on docling item objects; switching the splitter to walk items would change `content_hash` for every chunk, invalidating downstream embeddings/contextualizations on adoption and on every docling release that touches serializer output.
  **Converter-protocol leak.**
  Forces `ConversionArtifacts` to carry a `DoclingDocument` or `doc.json` field, which any future non-docling adapter (Marker, MinerU, plain-text passthrough, an LLM HTML cleaner) would have to leave empty or synthesize — inverting the data flow the `Converter` protocol exists to prevent.
  **Re-conversion semantics.**
  A docling upgrade that produces identical Markdown can still produce a different `doc.json`, churning chunk IDs (and downstream artifacts) for no observable change.
  Markdown-as-input preserves "Markdown unchanged → chunks unchanged" across docling upgrades; doc.json-as-input does not.
  **Efficiency is illusory.**
  `markdown-it-py` parses ~50KB of Markdown in ~5–20 ms; `DoclingDocument.model_validate_json` of the equivalent doc takes ~20–80 ms. "Parse once" is in fact slower, against a per-document conversion cost measured in seconds.
- _Reason for not selecting._
  Keeping Markdown as the inter-stage interlingua preserves the converter-agnostic `Converter` protocol from [ADR 002](./002-content-parsing.md), the `markdown_hash_xx64` change gate, and a stable citation surface for downstream stages.
  The alternatives trade those properties for marginal one-time implementation savings while taking on a permanent coupling to a young, fast-moving project's internal data model.
  Revisit if (a) a concrete downstream feature (e.g., PDF-viewer citation highlighting using page+bbox) requires structural metadata that Markdown does not carry, _and_ (b) the requirement justifies persisting a _trimmed_ `doc.json` (texts + prov only, image refs not base64) under its own ADR with its own change-gate field — kept separate from the chunking change.

#### Option 5: docling-core built-in chunkers (`HierarchicalChunker` / `HybridChunker`)

docling-core ships chunkers that operate on `DoclingDocument` natively.

- _Pros._
  Removes the splitter implementation outright; absorbs heading-tree walking, block classification, and structural-fidelity guarantees into a maintained upstream component.
- _Cons._
  Inherits the storage, schema-drift, and protocol-leak cons of Option 4, plus: `HybridChunker` requires a HuggingFace tokenizer and operates on token budgets, conflicting with the character-budget decision in this ADR.
  `HierarchicalChunker` has no size-budget logic at all.
  Neither implements the `chunk_id` / `content_hash` / `splitter_version` identity scheme this ADR requires; behavior and identity stability would be governed by docling-core's release cadence rather than this repo's `splitter_version`.
  As of docling-core v2.76 the chunker classes have not been formally marked stable (serializers received that designation in v2.29; chunkers have not).
- _Reason for not selecting._
  Outsourcing chunk identity to an unstable upstream is incompatible with the determinism contract this ADR builds the rest of the pipeline on.
  Revisit once docling-core publishes a stability guarantee on its chunker API _and_ the corpus has a concrete requirement that the upstream structural chunker satisfies but a heading-tree splitter does not.

## Implementation Details

- **Input.**
  A converted-document artifact (Markdown with heading structure preserved) plus its `doc_id` and `converted_artifact_id` from the conversion stage.
- **Output.**
  An ordered list of chunks, each carrying:
  - `chunk_id` — deterministic hash of `(doc_id, heading_path, ordinal, content_hash)`.
  - `content_hash` — text content hash, persisted as a separate field for diffability and audit (which axis moved when `chunk_id` changes: address or content).
  - `doc_id`, `heading_path`, `ordinal` — chunk address.
  - `text`
  - `char_count` (see size budget below)
  - provenance: `converted_artifact_id`, `markdown_hash_xx64`, `span` (stable offset into the converted artifact, sufficient to highlight the chunk in the source), `splitter_version`
- **Provenance contract.**
  `converted_artifact_id` ties each chunk to a specific `ConversionOutput` row from the conversion stage.
  `markdown_hash_xx64` — the conversion stage's existing content-addressable hash of the converted Markdown — is the document-level change gate: when unchanged across runs, the entire chunking run for that document is a no-op.
  `span` enables citation back to the source.
  `splitter_version` captures all splitter configuration (size budget, paragraph-split policy, edge-case rules) so a behavior change is detectable and can trigger targeted re-chunking.
- **Splitter.**
  Custom heading-aware Markdown splitter, written in-repo.
  Walks the heading tree; emits one chunk per heading body when within budget, paragraph-split otherwise.
  May use chonkie primitives (e.g., character counting, paragraph splitting) internally; MUST NOT pull in LangChain or LlamaIndex as transitive dependencies.
- **Splitter requirements (input domain).**
  The splitter MUST define behavior for the following cases; exact rules and tie-breakers live in the chunking spec:
  - **Pre-heading content** (frontmatter, abstracts, intros before any heading).
  - **Skipped heading levels** (e.g., `#` directly followed by `###`).
  - **Non-paragraph blocks** — code blocks, tables, lists, blockquotes, math blocks — must not split mid-block; may exceed size budget by exception.
  - **Headings inside non-section contexts** (inside list items, blockquotes, code blocks) — not section boundaries.
  - **Markdown flavor** — aligned with whatever the conversion stage emits; the splitter does not re-parse or alter Markdown semantics.
  - **Empty or heading-less documents** — degenerate but well-defined output (e.g., all chunks share `heading_path = []`).
  - **Embedded metadata / frontmatter** (YAML/TOML) — preserved as chunk metadata or a synthetic chunk; never silently dropped.
- **Size budget.**
  Expressed in characters, not tokens.
  Rationale: across major tokenizers (cl100k, llama, anthropic) a ~4 chars/token ratio for English holds within ~25%, which is adequate for chunk-size policy and keeps chunking independent of any specific tokenizer or embedding model.
  Exact budget values live in the chunking spec.
- **Pipeline shape.**
  Chunking adopts the conversion stage's event-sourcing pattern verbatim: state transitions go through a single `record_transition` write path; events are persisted to the same append-only log; chunk-lifecycle states reuse the existing event vocabulary where applicable and extend it where chunking-specific.
  Exact event taxonomy lives in the chunking spec.
- **Determinism.**
  Chunking is a pure function: same converted artifact + same `splitter_version` → same `chunk_id`s and `content_hash`es.
  No reconciliation against existing chunks is performed; any difference in `chunk_id`s between runs IS the invalidation signal.
- **What this stage does _not_ do.**
  No LLM calls.
  No materialized edges between chunks.
  No coreference resolution.
  No entity extraction.
  No contextualization.

## Future Work

These items are explicitly out of scope for the chunking stage but motivate the chunk contract above.

- **Contextualization stage (LLM enrichment).**
  Per [Anthropic's contextual retrieval approach](https://www.anthropic.com/news/contextual-retrieval), each chunk receives a short LLM-generated context blurb that situates it within the source document, so the chunk can be understood atomically at retrieval time.
  This is a separate, optional, event-sourced stage that takes chunks as input and emits enriched chunk artifacts; chunking itself does not depend on it.
- **Coreference resolution.**
  Within-chunk and (later) cross-chunk coreference resolution to improve atomic readability and graph linking.
  Likely a separate stage downstream of contextualization.
- **Knowledge graph construction.**
  A future stage builds entity nodes and relation edges; **nodes and edges reference chunks (the atomic zettel), not LLM-extracted claims**.
  This preserves the invariant that all graph content traces back to addressable, source-faithful chunks.
- **Structural graph materialization.**
  The latent `parent_of` / `precedes` relationships implied by `heading_path` and `ordinal` may be materialized as explicit edges when the graph stage lands.
  Until then, they remain derivable from chunk metadata.

The boundary between chunking and graph construction is intentionally narrow in this ADR: chunking ends when document-structure chunks are persisted.
Where contextualization, coreference, and graph enrichment slot in is left to their own ADRs.

## Related ADRs

- [002 - Content Parsing](./002-content-parsing.md)
- [004 - Model Provider (Framework)](./004-model-provider.md)
- [006 - Graph Construction and Entity Canonicalization](./006-graph-construction-entity-canonicalization.md)
- [007 - Embedding](./007-embedding.md)
- [008 - Indexing, Search, Retrieval](./008-index-search-retrieval.md)
