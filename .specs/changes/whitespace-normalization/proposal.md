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

## Open Questions (Resolved)

1. **Should code blocks be excluded from normalization?**

   **Yes.**
   Fenced code blocks and inline code are excluded.
   Confirmed by real-world fixtures (Tulu 3 Jinja templates, HuggingFace tensor alignment) that require multi-space preservation inside fences.

2. **Are there content types where normalization breaks expected formatting?**

   **No breakage found.**
   Empirically verified across 5,918 production conversion outputs (HTML and PDF).
   HTML is the highest-variance source but normalization is safe.

3. **What about trailing whitespace on lines?**

   **Strip unconditionally.**
   Resolved by Docling source-code and empirical investigation (2026-03-22).
   Confirmed empirically: converting HTML with `<br>` through the full Docling pipeline produces zero trailing spaces: (a) `MarkdownDocSerializer` has no code path that emits two-space Markdown hard breaks (`"  \n"`), and (b) The HTML backend replaces `<br>` with a plain `\n` (which collapses to a space in paragraph text), not a Markdown hard break.

   **Conclusion:** All trailing spaces in Docling output are conversion artifacts; stripping them is safe and produces cleaner, more stable hashes.
