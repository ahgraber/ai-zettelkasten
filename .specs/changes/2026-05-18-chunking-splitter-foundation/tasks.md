# Tasks: Chunking Splitter Foundation

## Calibration

Calibration informs the `DEFAULT_SIZE_BUDGET` constant and establishes baselines for parse latency and memory before the splitter is locked.
The calibration scripts (`measure.py`, `analyze.py`) are tracked under `scripts/chunking-calibration/`; their outputs (parquet/json + `findings.md`) are written to the gitignored `data/chunking-calibration/` directory.
The only outputs that land in `src/` are the chosen budget value and the recorded baseline numbers referenced from `data/chunking-calibration/findings.md`.

- [x] Write `scripts/chunking-calibration/measure.py` accepting a `--corpus <path>` argument; iterate over every `.md` file under that path, parse each with `markdown-it-py` (with frontmatter plugin), identify heading-body and paragraph regions, and emit one row per region with columns `doc_path`, `doc_size_chars`, `heading_depth`, `region_kind` ∈ {heading_body, paragraph}, `block_type`, `char_count`, `tiktoken_cl100k_count`.
  Persist rows to `data/chunking-calibration/regions.parquet`. (`block_type` added beyond the original schema to retain the markdown-it-py block name for paragraph rows.)
- [x] In `measure.py`, record per-document timing via `time.perf_counter` around read+parse+walk; persist to `data/chunking-calibration/per_doc_metrics.parquet` with columns `doc_path`, `doc_size_chars`, `parse_latency_ms`, `region_count`, `error`.
- [x] In `measure.py`, capture process-level memory: enable `tracemalloc` at start, snapshot peak via `tracemalloc.get_traced_memory()` every 100 documents and at end, plus a final `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` reading.
  Persist to `data/chunking-calibration/memory.json`.
- [x] Run `measure.py --corpus /Users/mithras/_code/bloom-search/data/benchmark/corpus` (5,674 documents) end-to-end; completed in 459s with 5,674/5,674 successful parses.
- [x] Compute decile distributions (p10, p25, p50, p75, p90, p95, p99) of `char_count` and `tiktoken_cl100k_count` separately for `region_kind = heading_body` and `region_kind = paragraph` across the full corpus. (`analyze.py`)
- [x] Compute the share of regions that fit within each of {1024, 1536, 2048, 3072, 4096, 6144, 8192, 12288, 16384} character budgets, for both heading-bodies and blocks; sensitivity-checked at 75%/90%/95%/99% thresholds.
  Finding: the heading-body 90% target is unreachable below ~8192 chars (forum-heavy corpus); the block distribution is the operative constraint (99.5% of blocks fit at 4096).
- [x] Compute parse-latency deciles (p50, p90, p99), total wall-clock for the full corpus, and the regression of `parse_latency_ms` against `doc_size_chars` (linear, R²=0.94).
  Compute peak `tracemalloc` allocation and peak RSS from `memory.json`.
- [x] Record in `data/chunking-calibration/findings.md`: chunk-size deciles (heading vs paragraph, in chars and tokens), fit-share tables, chosen budget with rationale, latency profile, memory profile, and outlier documents.
- [ ] Set `DEFAULT_SIZE_BUDGET = 4096` in `src/aizk/chunking/_version.py` (calibrated value; deferred to the Foundation group since the package skeleton is created there).

## Foundation

- [ ] Add `markdown-it-py` (with frontmatter plugin), `mdit-py-plugins`, and `chonkie` to `pyproject.toml`; run `uv lock`; verify `uv sync` succeeds.
- [ ] Create the `src/aizk/chunking/` package skeleton with `__init__.py`, `_version.py`, `datamodel.py`, and `splitter.py`.
- [ ] In `_version.py`, define `SPLITTER_VERSION: int = 1` and `DEFAULT_SIZE_BUDGET: int = 4096` (calibrated; see `data/chunking-calibration/findings.md`).
- [ ] Extract the `markdown_hash_xx64` algorithm (CRLF / CR → LF, `.strip()`, `.encode("utf-8")`, `xxhash.xxh64(...).hexdigest()`) from `aizk.conversion.utilities.hashing.compute_markdown_hash` into a shared helper accessible from both `aizk.conversion` and `aizk.chunking`.
  Keep `aizk.conversion.utilities.hashing.compute_markdown_hash` as a thin re-export so existing callers are untouched.

## Data model

- [ ] Define the `Chunk` pydantic model in `aizk.chunking.datamodel` with all spec-required fields: `chunk_id: str`, `content_hash: str`, `doc_id: str`, `heading_path: tuple[str, ...]`, `ordinal: int`, `text: str`, `char_count: int`, `converted_artifact_id: str`, `markdown_hash_xx64: str`, `span: tuple[int, int]`, `splitter_version: int`.
  Configure as frozen / immutable.
- [ ] Implement `derive_chunk_id(doc_id: str, heading_path: tuple[str, ...], ordinal: int, content_hash: str) -> str` using `json.dumps([doc_id, list(heading_path), ordinal, content_hash], separators=(",", ":"), ensure_ascii=False).encode("utf-8")` hashed with xxh64.
- [ ] Add module docstring in `aizk/chunking/__init__.py` summarizing public surface (`split`, `Chunk`, `SPLITTER_VERSION`, `DEFAULT_SIZE_BUDGET`) and the spec it implements.

## Splitter implementation

- [ ] Implement the public `split(markdown_text: str, *, doc_id: str, converted_artifact_id: str, markdown_hash_xx64: str, size_budget: int = DEFAULT_SIZE_BUDGET) -> list[Chunk]` function signature in `aizk.chunking.splitter`.
- [ ] Configure a `markdown-it-py` instance with `MarkdownIt("commonmark")` plus the frontmatter plugin from `mdit-py-plugins`; expose it as a module-level constant so token-walking helpers reuse one instance.
- [ ] Implement a token-stream walker that produces an in-memory heading tree: each node carries `heading_text`, `level`, and a list of body-region token slices.
- [ ] Implement a block classifier that maps token kinds (`fence`, `table_open`, `bullet_list_open`, `ordered_list_open`, `blockquote_open`, `math_block`, plus frontmatter) to `splittable: bool`.
- [ ] Implement span resolution from `token.map = [line_start, line_end]` to `(char_start, char_end)` character offsets in `markdown_text`; precompute a line-start offset table once per invocation.
- [ ] Implement chunk emission for an in-budget body region: one `Chunk` with the body's full text and span.
- [ ] Implement chunk emission for a splittable over-budget body: split on paragraph token boundaries; emit one `Chunk` per paragraph.
- [ ] Implement sub-paragraph fallback for an oversize paragraph via `chonkie.SentenceChunker` configured to `size_budget`; emit one `Chunk` per sentence-cluster.
- [ ] Implement emission for a non-splittable block: one `Chunk` containing the block in entirety; do not split mid-block even if it exceeds budget.
- [ ] Implement pre-heading-content emission: body regions before the first heading become chunks with `heading_path = ()` and `ordinal` values ordered before any titled chunks.
- [ ] Implement frontmatter emission: a detected frontmatter token becomes a chunk with `heading_path = ()` and `ordinal = 0`.
- [ ] Implement empty-heading-body handling: a heading with no body content emits no chunk for that empty body.
- [ ] Implement skipped-heading-level handling: `heading_path` reflects actual source nesting (no inferred intermediate levels).
- [ ] Implement heading-less-document handling: every chunk's `heading_path = ()`.
- [ ] Stamp `converted_artifact_id`, `markdown_hash_xx64`, `splitter_version`, and `span` on every emitted chunk; compute `content_hash` via the shared markdown-hash helper; compute `chunk_id` via `derive_chunk_id`.
- [ ] Re-export `split`, `Chunk`, `SPLITTER_VERSION`, `DEFAULT_SIZE_BUDGET` from `aizk/chunking/__init__.py`.

## Test fixtures

- [ ] Create `tests/chunking/fixtures/` with hand-authored `.md` files covering: pre-heading content, skipped heading levels, frontmatter, headings-in-fenced-code, headings-in-blockquote, headings-in-list, empty heading body, heading-less document, oversize paragraph, oversize non-splittable code block, multi-section document with several paragraphs per heading.
- [ ] Add at least one real docling-converted artifact (drawn from the calibration corpus) as a regression fixture under `tests/chunking/fixtures/regression/`.

## Determinism and purity tests (Requirement: Splitter is a deterministic pure function)

- [ ] Add `tests/chunking/test_determinism.py::test_two_invocations_field_equal` — calls `split()` twice on the same fixture and asserts both outputs are field-for-field equal.
- [ ] Add `tests/chunking/test_determinism.py::test_cross_process_chunk_ids_equal` — runs `split()` in two subprocesses (via `subprocess.run` with serialized input) and asserts `chunk_id` values are equal.
- [ ] Add `tests/chunking/test_determinism.py::test_no_io_during_split` — patches `builtins.open`, `socket.socket`, `pathlib.Path.read_*`, `urllib.request.urlopen`, and asserts no calls occur during `split()` execution.
- [ ] Add `tests/chunking/test_determinism.py::test_insensitive_to_env_and_time` — sets diverse env-var values and patches `time.time` / `datetime.now` to fixed values; asserts output is identical to the baseline.

## Identity tests (Requirement: Chunk identity is derived from address and content)

- [ ] Add `tests/chunking/test_identity.py::test_same_address_same_content_yields_same_chunk_id` — constructs two `derive_chunk_id` invocations with identical inputs; asserts equal output.
- [ ] Add `tests/chunking/test_identity.py::test_same_address_different_content_yields_different_chunk_id` — varies `content_hash` only; asserts different output.
- [ ] Add `tests/chunking/test_identity.py::test_different_address_same_content_yields_different_chunk_id` — varies one address axis (`heading_path` and separately `ordinal`); asserts different output in each case.

## Structural fidelity tests (Requirement: Structural fidelity to the source artifact)

- [ ] Add `tests/chunking/test_fidelity.py::test_body_regions_partitioned_across_chunks` — runs `split()` on a multi-section fixture; asserts that the union of `chunk.text` spans every body region of the source exactly once.
- [ ] Add `tests/chunking/test_fidelity.py::test_no_chunk_spans_heading_boundary` — for every chunk, asserts that the chunk's `span` lies entirely within a single heading body region.
- [ ] Add `tests/chunking/test_fidelity.py::test_chunk_order_reproduces_source_order` — sorts chunks by `(heading_path_in_document_order, ordinal)` and asserts the sequence of `span.start` values is monotonically non-decreasing.

## Size policy tests (Requirement: Size budget compliance with non-splittable block exception)

- [ ] Add `tests/chunking/test_size_policy.py::test_under_budget_body_one_chunk` — fixture whose heading body is below `size_budget`; asserts exactly one chunk emitted with `char_count <= size_budget`.
- [ ] Add `tests/chunking/test_size_policy.py::test_splittable_over_budget_paragraph_split` — fixture whose heading body exceeds budget but contains multiple within-budget paragraphs; asserts every emitted chunk has `char_count <= size_budget` and no paragraph is split across chunks.
- [ ] Add `tests/chunking/test_size_policy.py::test_oversize_non_splittable_block_kept_whole` — fixture whose heading body is a single fenced code block exceeding budget; asserts exactly one chunk emitted, `char_count > size_budget`, and the chunk text equals the full code block.
- [ ] Add `tests/chunking/test_size_policy.py::test_oversize_paragraph_sentence_fallback` — fixture with a single paragraph exceeding budget but with sentence boundaries; asserts every emitted chunk has `char_count <= size_budget` and the sentence-fallback path is exercised.
- [ ] Add `tests/chunking/test_size_policy.py::test_pathological_single_sentence_over_budget_warns` — fixture with a single sentence (no internal punctuation) exceeding budget; asserts one chunk emitted with `char_count > size_budget` and a structured warning is captured in logs.
- [ ] Add `tests/chunking/test_size_policy.py::test_non_splittable_block_never_partial` — fixture with a fenced code block among other paragraphs; asserts the code block appears in exactly one chunk in entirety and no other chunk contains any portion of the code block.

## Heading-path edge case tests (Requirement: Defined behavior for heading-path edge cases)

- [ ] Add `tests/chunking/test_heading_path.py::test_pre_heading_content_empty_path` — fixture whose first paragraph precedes any heading; asserts that paragraph is a chunk with `heading_path = ()` ordered before any titled chunks.
- [ ] Add `tests/chunking/test_heading_path.py::test_skipped_levels_actual_nesting` — fixture with `# A` then `### C`; asserts chunks under `C` have `heading_path = ("A", "C")`.
- [ ] Add `tests/chunking/test_heading_path.py::test_heading_in_fenced_code_not_boundary` — fixture with a `# foo` line inside a fenced code block; asserts the code block remains attached to the outer heading's `heading_path`.
- [ ] Add `tests/chunking/test_heading_path.py::test_heading_in_blockquote_not_boundary` — fixture with `> # foo` inside a blockquote; asserts the blockquote remains attached to the outer heading's `heading_path`.
- [ ] Add `tests/chunking/test_heading_path.py::test_heading_in_list_not_boundary` — fixture with `- ## bar` inside a list item; asserts the list remains attached to the outer heading's `heading_path`.
- [ ] Add `tests/chunking/test_heading_path.py::test_empty_heading_body_no_chunk` — fixture with a heading followed immediately by another heading; asserts no chunk is emitted for the empty body.
- [ ] Add `tests/chunking/test_heading_path.py::test_heading_less_document_empty_path` — fixture with no headings; asserts every chunk has `heading_path = ()`.
- [ ] Add `tests/chunking/test_heading_path.py::test_frontmatter_emitted_as_chunk` — fixture with YAML frontmatter and a following heading; asserts the frontmatter is emitted as a chunk with `heading_path = ()` and `ordinal` placing it before all other chunks.

## Provenance and version tests (Requirement: Provenance and version stamping)

- [ ] Add `tests/chunking/test_provenance.py::test_every_chunk_has_populated_provenance` — for every chunk emitted across the fixture suite, asserts `converted_artifact_id`, `markdown_hash_xx64`, `span`, and `splitter_version` are non-empty/non-default.
- [ ] Add `tests/chunking/test_provenance.py::test_provenance_uniform_across_invocation` — asserts all chunks emitted in one `split()` call share identical `converted_artifact_id`, `markdown_hash_xx64`, and `splitter_version`, equal to the inputs and `SPLITTER_VERSION` respectively.
- [ ] Add `tests/chunking/test_provenance.py::test_span_locates_chunk_in_source` — for every chunk, asserts `markdown_text[span.start:span.end]` contains the chunk's text (after the same normalization the splitter applies).
- [ ] Add `tests/chunking/test_provenance.py::test_provenance_per_emission_path` — parametrized across emission paths (in-budget body, paragraph-split, sentence-fallback, non-splittable block, pre-heading, frontmatter); asserts all four provenance fields are stamped on chunks from every path.

## Regression and version-discipline tests

- [ ] Add `tests/chunking/test_regression.py::test_fixture_suite_snapshot` — snapshots `[(input_path, [Chunk, ...]) for fixture in suite]` and asserts no drift; any output change must be accompanied by an explicit snapshot update commit AND a `SPLITTER_VERSION` bump.
- [ ] Add `tests/chunking/test_regression.py::test_version_bump_required_on_snapshot_change` — a CI-style check that fails if `SPLITTER_VERSION` is unchanged between commits but the snapshot test was updated; implementable as a pre-commit hook or a meta-test that compares the working tree against `HEAD`.

## Documentation

- [ ] Add a short README at `src/aizk/chunking/README.md` describing the public surface, linking to the spec at `.specs/specs/chunking/spec.md` (post-sync), and noting that decisions live in `docs/decision-record/005-chunking.md`.
- [ ] Update `CHANGELOG.md` under `[Unreleased]` with an `Added` entry: "Chunking splitter foundation (`aizk.chunking`): document-structure splitter, chunk data model, deterministic identity."
