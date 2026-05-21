# Design: Chunking Splitter Foundation

## Context

The chunking stage consumes converted Markdown artifacts produced by the existing conversion stage and emits ordered structural chunks for downstream stages (embedding, retrieval, future knowledge-graph construction).
This change implements only the foundational splitter and chunk data model; the pipeline change wires the splitter into an event-sourced worker.

Constraints shaping the design:

- The conversion stage emits Markdown via docling and runs `aizk.conversion.utilities.whitespace.normalize_whitespace` on the output before persistence (collapses excess spaces, normalizes 3+ newlines, strips trailing spaces, preserves fenced code blocks).
  Chunking consumes the already-normalized form; the splitter MUST NOT re-normalize the artifact body.
- The conversion stage already publishes a content-addressable hash of the converted Markdown as `markdown_hash_xx64` (xxh64 hex digest, 16 chars) on `ConversionOutput`, computed by `aizk.conversion.utilities.hashing.compute_markdown_hash` with CRLF→LF + outer-strip normalization for hashing.
  Identity and change-detection in the chunking stage reuse the same hash algorithm and normalization pattern.
- The chunking stage's `chunk_id` derivation must be cross-process stable so that a chunk's identity is reproducible without consulting prior state.
- LangChain and LlamaIndex are excluded as splitter dependencies.
  `chonkie` is permitted as an internal primitive for sub-paragraph fallback.
- Project versioning convention for persisted schema/output variants is a monotonically increasing integer starting at 1 (cf. `ConversionJob.payload_version` and `ConversionOutput.payload_version`); `splitter_version` follows the same convention.

## Decisions

### Decision: Markdown parser

**Chosen:** [`markdown-it-py`](https://github.com/executablebooks/markdown-it-py) on the CommonMark preset, with the GFM `table` rule enabled, the `dollarmath` plugin (`$$` math blocks), and the YAML frontmatter plugin.
TOML (`+++`) frontmatter is detected by a small leading-fence check in the splitter (the frontmatter plugin recognizes only `---`).

**Rationale:**

- **The non-splittable block contract requires the parser to recognize those blocks.**
  The spec keeps tables and math blocks whole; under bare CommonMark a pipe table tokenizes as a paragraph and `$$` math as a paragraph, so the over-budget paragraph path would split them across chunks.
  Enabling the `table` rule and `dollarmath` makes them emit `table_open` / `math_block` tokens, which the block classifier maps to non-splittable — the only way to honor the contract without hand-rolling block detection.

- **TOML frontmatter is part of the contract.**
  The spec admits YAML _or_ TOML frontmatter; the frontmatter plugin only handles `---`, so a leading `+++ … +++` block is detected directly and emitted as a frontmatter chunk, preventing heading-shaped lines inside it from being read as section boundaries.

- **Code-block false positives are a real bug class, not a rare edge case.**
  Any fenced code block containing a `#`-prefixed line at column 0 (Python comments, shell scripts, C preprocessor, Markdown about Markdown) reads as a level-1 heading to a naïve `#{1,6}\s+` regex.
  Pre-masking fenced code regions before scanning is the first step of writing a Markdown block parser; doing so honestly (also handling blockquote continuation, list continuation, indented code, setext headings, math blocks, HTML blocks, frontmatter ambiguity) reproduces `markdown-it-py` poorly.

- **Source maps come for free.**
  Each token carries `.map = [line_start, line_end]`; the splitter resolves these to character offsets directly.
  A regex approach must track positions through mask/unmask round-trips and across multiple passes, which is silently wrong until a test pins it.

- **Pure-Python with negligible transitive surface.**
  Dependency is ~200 KB, transitive surface = `mdurl`, no build tooling.
  The proposal's "no third-party splitter" intent is directed at LangChain / LlamaIndex (framework gravity); a small CommonMark parser is not in the same category.

- **The frontmatter plugin removes a real ambiguity.**
  YAML frontmatter contains `#` (YAML comment syntax) that the splitter must not confuse with headings.
  A typed frontmatter token eliminates the hand-rolled detection.

- **Picking once is cheaper than swapping later.**
  If the splitter ships with regex and later swaps to a parser, `splitter_version` bumps and every `chunk_id` changes — embeddings, contextualizations, and graph all re-run from scratch.
  The whole point of the chunk-identity design is to make content changes cheap, not mechanism changes.

- **Docling output is heuristic.**
  Docling makes its own decisions about heading detection, table structure, and code-vs-text discrimination from PDF/HTML inputs.
  "Clean Markdown out" is a bet, not a guarantee; the parser is what makes the splitter robust to that bet being wrong.

**Alternatives considered:**

- **Naïve regex (`#{1,6}\s+` plus `\n\n` paragraph split).**
  Smallest possible footprint, no dependency.
  Fails on the code-block case above; fails on `> # foo` (blockquoted heading-shaped text); fails on setext headings if any flavor produces them.
  Calibration measures chunk-size distribution, not splitter correctness — wrong chunks pass calibration silently.
  Acceptable only for a corpus that is guaranteed well-formed Markdown (e.g., hand-authored notes with no PDF conversion in the pipeline).
- **`mistletoe`:** AST instead of tokens; slightly nicer for tree walks but smaller maintainer base and no frontmatter handling out of the box.
- **`cmarkgfm`:** C extension, fast, but adds a build dependency and does not expose source positions cleanly.
- **Hand-rolled walker:** reproduces years of Markdown edge-case handling at cost; rejected on YAGNI grounds.

### Decision: Hash function for chunk_id and content_hash

**Chosen:** xxh64 (the same algorithm the conversion stage already uses for `markdown_hash_xx64`), emitting a 16-char hex digest.

**Rationale:**

- Project consistency: the conversion stage already depends on `xxhash` for `markdown_hash_xx64`; reusing it adds no new dependency.
- 64-bit collision space is adequate for this corpus.
  For a personal Zettelkasten on the order of 10⁵ chunks, birthday-paradox collision probability is below 10⁻⁹.
- Speed: non-cryptographic; chunk hashing is not on a security-sensitive path.

**Alternatives considered:**

- BLAKE2b: cryptographic strength is not required and increases CPU cost; rejected on YAGNI.
- SHA-256: same; widely understood but oversized for this purpose.
- SipHash: keyed; would violate cross-process stability unless the key were committed to source, defeating its purpose.

### Decision: Canonical serialization for `chunk_id` inputs

**Chosen:** Hash the UTF-8 encoding of `json.dumps([doc_id, list(heading_path), ordinal, content_hash], separators=(",", ":"), ensure_ascii=False)`.

**Rationale:**

- JSON canonical form gives unambiguous serialization for the four fields without bespoke escaping logic.
- Compact separators eliminate whitespace ambiguity.
- `ensure_ascii=False` keeps Unicode heading text as UTF-8 in the hash input; the outer `.encode("utf-8")` step is the byte-level commitment.
- Fields ordered `[doc_id, heading_path, ordinal, content_hash]` deterministically — any reordering bumps `splitter_version`.

**Alternatives considered:**

- Length-prefixed concatenation: more compact but invents a new encoding the team has to remember.
- NUL-separated concatenation: brittle if heading text ever contains a NUL byte (unlikely but not enforceable at the type level).
- msgpack: extra dependency for a one-line serialization need.

### Decision: `content_hash` normalization

**Chosen:** Reuse the conversion stage's hash-time normalization: CRLF / CR → LF, then `.strip()` the chunk text as a whole, then `.encode("utf-8")` and hash.
This is exactly the body of `compute_markdown_hash` from `aizk.conversion.utilities.hashing`; extract it (or a small wrapper) to a shared utility so both stages call the same helper.

The chunk's `text` field stores the chunk content **before** hash-time stripping, i.e., exactly as carved from the conversion stage's already-whitespace-normalized Markdown.
This keeps `content_hash` reproducible from `text` via the shared helper and preserves the original spacing inside the chunk body.

**Rationale:**

- The conversion stage already runs `normalize_whitespace` on the artifact before persistence (collapses excess spaces, normalizes newline runs, strips trailing whitespace, preserves code fences).
  The chunking stage receives an already-normalized artifact; per-chunk normalization is redundant.
- Matching the conversion stage's hash flow keeps the algorithm consistent across stages and avoids per-stage drift.
- Unicode NFC normalization is not done in the conversion stage and is not added here; if NFC ever becomes necessary it is added to the shared helper and both stages bump together.

**Alternatives considered:**

- Per-stage normalization (the original design): added NFC and per-line rstrip on top of conversion's normalization; rejected as duplicated work that drifts across stages.
- No normalization: makes the hash brittle against trailing-whitespace edits at chunk boundaries.

### Decision: `span` representation

**Chosen:** `span` is a `(start, end)` pair of character (codepoint) offsets into the post-normalization Markdown artifact (inclusive start, exclusive end, Python-slice semantics).
The text consumed by the splitter and the text addressed by `span` are the same value; resolving `span` is `artifact_text[start:end]`.

**Rationale:**

- The splitter operates on Python `str` objects directly; character offsets are the splitter's natural unit and require no encoding translation.
- The conversion stage persists the normalized Markdown artifact; downstream consumers read it back as `str` and can resolve spans by simple slicing.
- `markdown-it-py` exposes line-level source maps; the splitter resolves line ranges to character offsets against the artifact text once per chunk.

**Alternatives considered:**

- UTF-8 byte offsets: unambiguous across encodings but require constant `.encode()` / `.decode()` plumbing in a Python-only pipeline.
- Line/column tuples: human-readable but require the original text to interpret and complicate slicing.

### Decision: Default size budget

**Chosen:** 4096 characters (≈ 930 cl100k tokens at the corpus-measured 4.4 chars/token ratio), calibrated against the full benchmark corpus (5,674 documents, 1.08M regions).

**Rationale:**

- Calibration measured heading-body and top-level-block (paragraph) size distributions in both characters and tiktoken cl100k tokens.
  The corpus is discussion/forum-heavy, so heading bodies are routinely 4k–600k chars and a "fit 90% of sections whole" target is unreachable below ~8192 chars — chasing it would force coarse, non-atomic chunks.
- The operative constraint is the block distribution: at 4096 chars, 99.5% of top-level blocks fit whole, so sentence-fallback remains the rare exception the spec contract frames it as.
- 4096 chars ≈ 930 tokens sits just under a 1024-token embedder ceiling with headroom for denser content (code, tables) that runs below the 4.4 chars/token median — chunks stay embeddable without truncation by long-context sentence-transformer / OpenAI-class models.
- At 4096, 77% of heading bodies are kept whole; longer sections split into block-sized chunks (intended behavior — `heading_path` provenance preserves the section relationship across the split).
- The budget is captured by `splitter_version`; any future change to the default bumps the version and is auditable.

Full calibration record: `data/chunking-calibration/findings.md`.

**Alternatives considered:**

- 2048 chars (~465 tokens): keeps 98.5% of blocks whole and fits 512-token embedders, but fragments sections more aggressively (only 61% of heading bodies whole).
  Viable if a short-context embedder is chosen downstream.
- 8192 chars (~1860 tokens): keeps 90% of heading bodies whole, but requires a long-context embedder and yields the coarsest, least-atomic chunks.
- Configurable per-call without a baked-in default: pushes the decision to every caller; rejected in favor of one well-grounded default value (overridable via the `size_budget` parameter when needed).

### Decision: `heading_path` representation

**Chosen:** `heading_path` is a `tuple[str, ...]` of heading text values, ordered from outermost to innermost.
The empty tuple `()` represents the document root.

**Rationale:**

- Tuples are immutable and hashable, suitable for use as part of the `chunk_id` hash input and for stable address keys.
- String elements preserve heading text exactly as it appears in the source (post-normalization), supporting human-readable addressing.
- Duplicate heading text within the same parent (two `## Conclusion` sections under the same `# Section`) is disambiguated by `ordinal`, not by mangling heading text.

**Alternatives considered:**

- `/`-joined string: requires escape policy for `/` in heading text.
- List of `(level, text)` tuples: redundant — level is implicit from position.

### Decision: `ordinal` is per-heading-body, not document-global

**Chosen:** `ordinal` is a non-negative integer that counts emitted chunks within the same `heading_path`, starting from 0.
Chunks under different `heading_path` values restart their `ordinal` at 0.

**Rationale:**

- Localizes invalidation: inserting content under one heading shifts `ordinal` values (and thus `chunk_id`s) only for chunks under that same heading.
- A document-global scheme would shift every later `ordinal` on any insertion, invalidating most chunks on every edit.

**Alternatives considered:**

- Document-global ordinal: simpler ordering retrieval but maximally edit-fragile.
- Stable string IDs derived from heading text + content prefix: too brittle, hard to define collision rules.

### Decision: Sub-paragraph fallback for over-budget paragraphs

**Chosen:** When a single paragraph exceeds the size budget, the splitter applies sentence-level fallback using `chonkie`'s `SentenceChunker` configured to the same character budget.
If a single sentence still exceeds the budget, the sentence is emitted as one chunk that exceeds the budget; this is logged as a structured warning but is not a hard error.

A paragraph that carries an inline construct (`link_open`, `image`, `code_inline`, or `math_inline`) is exempted from sentence fallback and emitted whole (over budget, with a structured warning).
`SentenceChunker` splits on sentence punctuation with no awareness of Markdown, and markdown-it exposes no source offsets for inline children, so a split point can land inside a link/image/code/math span and produce a dangling `](url)` or half a code span.
The presence of such a construct is detectable by inline token type (no offsets needed), so the paragraph is treated as non-splittable rather than risk emitting broken Markdown.

**Rationale:**

- The spec contract permits over-budget chunks only for non-splittable blocks.
  Sentence fallback closes the gap for splittable paragraphs that paragraph-level splitting cannot satisfy on its own.
- `chonkie` is already permitted as an internal primitive and avoids hand-rolling sentence boundary detection.
- The single-sentence overflow case is a known degenerate input (e.g., a 5000-character sentence with no internal punctuation); accepting graceful degradation here is preferable to mid-word splitting, which would produce non-meaningful chunks.
- Bare URLs and autolinks survive sentence fallback already (no period-space inside them); the inline-construct guard covers the cases that do not — links, images, code spans, and inline math whose visible text contains sentence punctuation.

**Alternatives considered:**

- Word-level fallback: produces semantically poor chunks; rejected.
- Reject documents with oversize sentences: too aggressive for a personal-corpus tool.
- Hand-rolled sentence splitter: reinvents `chonkie`'s primitive.
- Inline-construct-aware boundary snapping: would require regex-scanning paragraphs for Markdown inline syntax (markdown-it gives no inline offsets) — the same hand-rolled inline parsing rejected for block detection; the token-type guard achieves safety without it, at the cost of leaving those rare paragraphs over budget.

### Decision: `splitter_version` as monotonic integer

**Chosen:** `splitter_version` is a non-negative integer maintained as a module-level constant (`SPLITTER_VERSION: int = 1`).
It is bumped (+1) on any change that alters observable output for any input: regex behavior, hash function, canonical serialization, content_hash normalization, size budget default, edge-case handling.
Pure refactors that produce byte-identical output for a fixed regression fixture do not bump it.

**Rationale:**

- Matches the project's existing schema-versioning convention (`ConversionJob.payload_version`, `ConversionOutput.payload_version` — both monotonic ints starting at 1).
- Trivial to compare, store, and index; no parsing required.
- Internal version, not externally published — semver's MAJOR / MINOR / PATCH distinction adds no signal.
- The "regression fixture must pass byte-for-byte to avoid a bump" rule is enforceable by a single test that fails closed.

**Alternatives considered:**

- Semver string (`"0.1.0"`): familiar but the MAJOR/MINOR/PATCH distinction has no internal consumer to act on it.
- Git SHA prefix: changes on every commit, including no-op refactors; too noisy.
- Date-based: doesn't encode behavior, only timing.

## Architecture

The splitter is a pure function in the `aizk.chunking` package.
No persistence, no I/O, no scheduling.

```text
            ┌─────────────────────────────────────────────────────────┐
            │ aizk.chunking.split(markdown_text, doc_id,              │
            │                     converted_artifact_id,              │
            │                     markdown_hash_xx64,                 │
            │                     *, size_budget=DEFAULT) -> [Chunk]  │
            └─────────────────────────────────────────────────────────┘
                                       │
                                       ▼
            ┌─────────────────────────────────────────────────────────┐
            │ 1. Parse via markdown-it-py (with frontmatter plugin)   │
            │ 2. Walk token stream, build heading tree                │
            │ 3. For each heading body region:                        │
            │      classify blocks (splittable / non-splittable)      │
            │      resolve token line-maps to char-offset span        │
            │ 4. Emit chunks:                                         │
            │      • body within budget       -> 1 chunk              │
            │      • body over budget         -> paragraph-split      │
            │      • paragraph over budget    -> sentence-split       │
            │        (chonkie SentenceChunker)                        │
            │      • non-splittable block     -> 1 chunk (may         │
            │        over-budget; spec-permitted exception)           │
            │ 5. For each emitted chunk:                              │
            │      compute content_hash via shared markdown-hash      │
            │        helper (CRLF->LF + outer strip + xxh64)          │
            │      compute chunk_id (xxh64 over JSON canonical of     │
            │        [doc_id, heading_path, ordinal, content_hash])   │
            │      stamp provenance + splitter_version                │
            └─────────────────────────────────────────────────────────┘
                                       │
                                       ▼
                              ordered list of Chunk
```

Package layout:

```text
src/aizk/chunking/
├── __init__.py        # public re-exports: split, Chunk, SPLITTER_VERSION
├── datamodel.py       # Chunk pydantic model
├── splitter.py        # split() and internal helpers
└── _version.py        # SPLITTER_VERSION constant
```

## Risks

- **Markdown-flavor drift between docling and `markdown-it-py`.**
  Docling may emit Markdown that `markdown-it-py` parses differently than intended (e.g., subtle GFM table behaviors, unconventional spacing).
  Mitigation: include a regression fixture set in tests using real docling-converted artifacts; any parser disagreement surfaces as a fixture diff before reaching production.
- **`splitter_version` is forgotten on a behavior change.**
  The version string is human-maintained; a contributor could change normalization without bumping it, silently invalidating downstream consistency.
  Mitigation: a CI test snapshots `(input -> chunks)` mappings for a small fixture suite; any output change without a version bump fails the test.
- **xxh64 collisions.**
  Non-cryptographic 64-bit hashes admit collisions in principle.
  Mitigation: for a personal corpus, collision probability is negligible; if the corpus grows to ~10⁸ chunks the decision should be revisited.
- **Pathological sentence with no boundaries exceeds budget.**
  A single sentence longer than the size budget produces an over-budget chunk despite the spec's contract.
  Mitigation: log a structured warning when this occurs; document the limitation; revisit if it appears in real corpus inputs.
- **Heading text containing characters that confuse downstream consumers.**
  `heading_path` elements are arbitrary user strings (could contain `/`, NUL, quotes, control characters).
  Mitigation: canonical serialization uses JSON, which escapes all problematic characters; downstream consumers MUST treat `heading_path` as opaque structured data, not as a path string to be joined.
