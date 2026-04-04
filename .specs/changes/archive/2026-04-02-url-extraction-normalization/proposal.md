# URL Extraction and Normalization Change Proposal

**Date:** 2026-04-01 **Change Name:** `url-extraction-normalization`

## Intent

Improve URL extraction and normalization to ensure idempotent deduplication and alignment with karakeep's URL semantics.
This enables ai-zettelkasten to reliably deduplicate URLs despite formatting variations (www prefix, UTM params, GitHub URL variants) and prepares the system to function independently from karakeep while maintaining URL handling consistency.

## Scope

### In

- **`src/aizk/utilities/url_utils.py`**

  - Improve `extract_urls()` with two-phase extraction: markdown links first, then bare URLs
  - Add `extract_domain()` utility function
  - Add `standardize_github()` function for GitHub URL canonicalization
  - Update `normalize_url()` to strip `www.` prefix (aligns with karakeep expectations)
  - Add `GITHUB_DOMAINS` constant for classification

- **Tests:** `tests/conversion/unit/test_url_utils.py` and `tests/utilities/test_url_utils.py`

  - Test idempotent deduplication: applying `normalize_url()` twice yields identical result
  - Test same URL in different formats deduplicates to same normalized form
  - Test cases for: www stripping, GitHub URL variants, UTM params, SafeLinks

### Out

- Markdown parsing beyond basic markdown link extraction (already handled via `extract_markdown_urls()`)
- Social media domain classification (deferred)
- Full ai-treadmill processor pipeline (out of scope for this change)
- Integration of `standardize_github()` into `normalize_url()` (defer to future refinement)

## Approach

1. **Two-phase URL extraction:** Extract markdown links `[text](url)` with precise boundaries first, then extract bare URLs from remaining text.
   Reduces false positives from balanced bracket constraints.

2. **GitHub standardization:** Add `standardize_github()` to canonicalize GitHub URLs:

   - `raw.githubusercontent.com` → `github.com`
   - Strip branch/ref info to reach repo root
   - Normalize gist URLs to canonical form

3. **www-aware normalization:** Update `normalize_url()` to strip leading `www.` before lowercasing domain, ensuring `www.example.com` and `example.com` normalize identically.

4. **Extract domain utility:** Add `extract_domain()` function for classifiers and deduplication logic to reliably pull domain portion.

5. **Comprehensive testing:** Verify idempotence (running `normalize_url()` twice = once) and deduplication (different URL formats resolve to same normalized form).

## Open Questions

None — scope and approach are locked.

## Dependencies

- No new external dependencies required
- Uses existing: `urllib.parse`, `validators`, `pydantic.HttpUrl`, `re`
