# Whitespace Normalization Design

## Context

The conversion worker runs Docling on source content (HTML, PDF) and produces Markdown output via `_run_conversion()` in `worker.py`.
This Markdown is written to `output.md`, hashed for deduplication, and uploaded to S3.

Docling's conversion pipelines sometimes produce extraneous whitespace:

- Multiple consecutive spaces in inline content
- Extra blank lines between sections
  This creates consistency issues when reruns of the same content produce minor whitespace differences that should not affect deduplication.

## Decisions

### 1. Normalize before hashing and writing

**Decision:** Apply normalization to `markdown_text` immediately after conversion completes (inside `_run_conversion()`), before both file I/O and hash computation.

**Rationale:**

- Ensures content hash is stable across identical inputs
- Simplest point of integration; single place to apply the transformation
- Prevents upload of non-normalized artifacts that should be identical semantically

**Alternatives considered:**

- Normalize only on read/comparison (requires downstream changes, more complex)
- Add a separate normalization phase after hashing (requires recomputation, inefficient)
- Make normalization configurable per content type (adds complexity; assume it's universally safe)

### 2. Preserve intentional whitespace in code blocks

**Decision:** Code blocks (fenced with triple backticks) and inline code should NOT have their internal whitespace modified.

**Rationale:**

- Code formatting and indentation are semantically important
- Collapsing spaces in code would break examples and snippet readability
- Docling typically outputs code blocks correctly; preserve them as-is

**Implementation approach:**

- Normalize outside code blocks only
- Use regex or state-machine parsing to identify and skip code fence regions

**Alternatives considered:**

- Apply normalization uniformly (simple but breaks code examples)
- Strip all leading/trailing whitespace (too aggressive)

### 3. Test-driven approach to edge cases

**Decision:** Comprehensive test suite covering basic rules plus edge cases discovered during implementation.

**Rationale:**

- Whitespace handling is deceptively complex in Markdown
- Tests reveal whether naive collapsing breaks structured content (tables, lists, frontmatter)
- Validates assumptions about code block preservation

**Test categories:**

1. **Basic normalization:** single-space and dual-newline collapsing
2. **Code block preservation:** fenced blocks, inline code
3. **Structured content:** tables, lists, indented quotes, YAML frontmatter
4. **Edge cases:** empty document, single-line content, document boundaries
5. **Content types:** HTML-derived output, PDF-derived output, arXiv, GitHub

## Architecture

### Input/Output Flow

```text
Docling conversion output
    ↓
markdown_text (from convert_html / convert_pdf)
    ↓
normalize_whitespace(markdown_text) [NEW]
    ↓
write to output.md
↓ (same normalized text)
compute_markdown_hash
↓
store ConversionOutput record
```

### Implementation Location

- **Utility function:** `aizk/conversion/utilities/whitespace.py` → `normalize_whitespace(text: str) -> str`
- **Integration point:** `aizk/conversion/workers/worker.py` → `_run_conversion()` at line 322, before `markdown_file.write_text()`

## Risks & Mitigations

| Risk                                            | Probability | Impact | Mitigation                                                                                           |
| ----------------------------------------------- | ----------- | ------ | ---------------------------------------------------------------------------------------------------- |
| Normalization breaks Markdown table alignment   | Medium      | High   | Comprehensive unit tests covering Markdown table syntax; validate in test suite                      |
| Code blocks lose critical whitespace            | Medium      | High   | Explicit code-block-aware parsing; test coverage                                                     |
| Leading/trailing newlines are over-collapsed    | Medium      | Low    | Clear rules in implementation (e.g., preserve final newline); test boundaries                        |
| Hash stability is broken for existing artifacts | Low         | High   | Hash computed post-normalization; existing artifacts unaffected unless re-run; document in changelog |
| Performance impact from regex parsing           | Low         | Low    | Normalize is single-pass linear scan; minimal overhead                                               |

## Future Considerations

- If whitespace rules need to vary by content type, move normalization logic into converter pipeline classes
- Monitor S3 artifact deduplication to verify hash stability improvement
- Consider adding configurable whitespace profiles (strict vs. permissive) if downstream consumers differ
