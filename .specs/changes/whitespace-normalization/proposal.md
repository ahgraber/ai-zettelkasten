# Whitespace Normalization Proposal

## Intent

Remove formatting artifacts and extraneous whitespace from Markdown output produced by Docling conversion.
The conversion pipeline sometimes produces multiple consecutive spaces or excessive blank lines.
Normalizing these improves artifact consistency and readability, and ensures that content hash remains stable across reruns when only whitespace differs.

## Scope

**In:**

- Normalizing Markdown text before writing to `output.md` during the conversion worker's `_run_conversion` phase
- Specific rules: collapse multiple consecutive spaces to single space; collapse 3+ newlines to 2 newlines
- Comprehensive test coverage to identify edge cases and determine if this is truly simple or reveals other issues

**Out:**

- Modifying how hashes are computed or stored (hash computed after normalization, so semantically the same)
- Changing figure extraction, metadata, or other artifact handling
- Whitespace normalization in other contexts (only applies to final Markdown output)

## Approach

1. Create a `normalize_whitespace()` utility function in `aizk.conversion.utilities` that:

   - Collapses multiple consecutive spaces (2+) to a single space
   - Collapses 3+ consecutive newlines to exactly 2 newlines
   - Preserves intentional formatting (indentation in code blocks, structured lists)

2. Apply normalization in `_run_conversion()` in `worker.py` immediately after conversion completes but before writing to disk and computing hash

3. Add comprehensive unit tests covering:

   - Basic space and newline collapsing
   - Code blocks and indentation preservation
   - Structured data (YAML/JSON frontmatter, tables)
   - Edge cases: empty documents, single-line content, highly nested structures

4. Verify through integration tests that normalized output round-trips through downstream consumers correctly

## Open Questions

1. Should code blocks (fenced with backticks) be excluded from normalization to preserve internal indentation?

   - Current assumption: Yes, preserve intentional whitespace within code blocks
   - Will be determined by test results

2. Are there content types (arXiv, GitHub READMEs, etc.) where whitespace normalization breaks expected formatting?

   - Current assumption: Normalization is content-agnostic; tests will reveal any breakage

3. What about leading/trailing whitespace on lines or at document boundaries?

   - Current assumption: Collapse runs but preserve one newline at document end; will validate in tests
