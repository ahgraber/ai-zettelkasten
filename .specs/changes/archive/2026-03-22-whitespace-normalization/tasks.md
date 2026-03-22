# Whitespace Normalization Tasks

## Task 0: Mine database for real-world test fixtures

**Description:** Extract representative markdown samples from existing conversion database to use as test fixtures.

**Context:** The conversion database (`data/conversion_service.db`) contains 5,918 conversion outputs.
Actual markdown from conversions reveals edge cases better than synthetic test cases.

**Acceptance criteria:**

- Query `conversion_outputs` table for sample conversions:
  - 3+ HTML pipeline outputs with different content types (arXiv, GitHub, general web)
  - 3+ PDF pipeline outputs with different content types
  - Preferably with varying levels of complexity (simple, moderate, complex)
- Fetch corresponding markdown from S3 (using markdown_key field in database)
- Store as test fixtures in `tests/conversion/fixtures/real_markdown/` directory
- Include metadata (source type, pipeline, title) for each fixture
- Document any observations about whitespace patterns found (e.g., "PDFs often have 3+ newlines between sections")

**Notes:** This informs the test cases in Task 2 and may reveal edge cases the team hadn't anticipated

---

## Task 1: Create whitespace normalization utility

**Description:** Implement `normalize_whitespace(text: str) -> str` in a new module `aizk/conversion/utilities/whitespace.py`.

**Acceptance criteria:**

- Function collapses 2+ consecutive spaces to single space
- Function collapses 3+ consecutive newlines to exactly 2 newlines
- Code blocks (fenced with triple backticks) preserve internal whitespace exactly
- Inline code (single backticks) does not have spaces collapsed around it
- Function is exported in `aizk/conversion/utilities/__init__.py`
- Function includes a docstring with examples and edge case notes

**Testing:** Will be verified by Task 2 unit tests

---

## Task 2: Write comprehensive unit tests for whitespace normalization

**Description:** Create `tests/conversion/unit/test_whitespace.py` with test cases covering normalization behavior.

**Acceptance criteria:**

- **Basic normalization tests:**

  - `test_collapse_multiple_spaces()` — verify 2, 3, 10 spaces → 1 space
  - `test_collapse_newlines_two()` — verify 3, 4, 5 newlines → 2 newlines
  - `test_preserve_single_spaces()` — single spaces unchanged
  - `test_preserve_single_newlines()` — single/double newlines unchanged

- **Code block preservation tests:**

  - `test_code_block_indentation_preserved()` — indentation in fenced blocks unchanged
  - `test_inline_code_spaces_preserved()` — spaces around and inside backticks unchanged
  - `test_multiline_code_block()` — indentation across multiple lines in code block preserved

- **Structured content tests:**

  - `test_markdown_table_alignment()` — pipe-aligned tables remain properly formatted
  - `test_bullet_list_indentation()` — list indentation preserved
  - `test_numbered_list_indentation()` — numbered list indentation preserved
  - `test_blockquote_indentation()` — blockquote indentation preserved
  - `test_yaml_frontmatter()` — frontmatter spacing preserved

- **Edge case tests:**

  - `test_empty_string()` — empty input returns empty
  - `test_single_line_content()` — no newlines handled correctly
  - `test_leading_trailing_whitespace()` — document boundaries handled correctly
  - `test_document_final_newline()` — trailing newline preserved (one)

- **Real-world test fixtures from database (5,918 existing conversions):**

  - Mine `data/conversion_service.db` (conversion_outputs table) for representative markdown samples
  - Fetch actual markdown from S3 for each pipeline type (HTML, PDF) and content type (arXiv, GitHub, general)
  - Create test fixtures combining real outputs with synthetic whitespace variations
  - Tests: `test_real_html_output_*`, `test_real_pdf_output_*`, `test_real_arxiv_*`, `test_real_github_*`
  - Verify normalization does not corrupt real-world markdown structure and formatting

**Testing:** All tests pass; coverage ≥ 95% for whitespace module; at least 3 real-world fixtures per content type

---

## Task 3: Integrate whitespace normalization into conversion worker

**Description:** Modify `aizk/conversion/workers/worker.py` to apply normalization before writing output.md.

**Acceptance criteria:**

- In `_run_conversion()` function, apply `normalize_whitespace()` to `markdown_text` before line 322 (`markdown_file.write_text()`)
- Hash computation uses normalized text (no code change required; same variable)
- Import added: `from aizk.conversion.utilities.whitespace import normalize_whitespace`
- Existing unit tests for worker still pass

**Code location:** `src/aizk/conversion/workers/worker.py` around line 322, before write

---

## Task 4: Run existing integration tests and verify no breakage

**Description:** Execute full conversion test suite to ensure normalization does not break downstream processing.

**Acceptance criteria:**

- `pytest tests/conversion/` passes without failures
- All conversion output artifacts (markdown, figures, manifest) are valid
- Hashes computed for normalized output match expected values
- No performance regression (if benchmarks exist)

**Notes:** Integration tests use real (or realistic) KaraKeep bookmark data and Docling conversion; normalization must not break round-trip correctness

---

## Task 5: Write integration test validating whitespace normalization end-to-end

**Description:** Add a test in `tests/conversion/integration/` that verifies a bookmark processed twice produces identical output including whitespace.

**Acceptance criteria:**

- Create a test bookmark with known content that Docling might produce with whitespace variation
- Process it twice through full conversion pipeline
- Verify `output.md` content is byte-identical both times
- Verify content hash is identical both times

**Notes:** This test confirms the stability guarantee promised by the feature

---

## Task 6: Document changes and update specs in code

**Description:** Update technical notes in `conversion-worker/spec.md` baseline if the main spec is still referenced; no change needed if delta specs are the source of truth.

**Acceptance criteria:**

- If baseline spec is synchronized with delta, add note about whitespace normalization to Technical Notes section under markdown output handling
- Commit message references this change
