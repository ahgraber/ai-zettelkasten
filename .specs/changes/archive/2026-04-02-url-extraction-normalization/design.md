# URL Extraction and Normalization Design

## Context

### Existing State

- Current `url_utils.py` has basic extraction via regex-only approach
- Validation is solid (Pydantic `HttpUrl` + validators library)
- Limited normalization: lowercases netloc, sorts query params, strips fragment
- No www-stripping (inconsistent with karakeep's deduplication)
- No GitHub canonicalization
- No robust boundary detection for markdown extraction

### Why This Matters

ai-zettelkasten imports bookmarks from karakeep (which already normalizes URLs).
To function independently while maintaining semantic consistency, aizk must:

1. Normalize URLs identically to how karakeep does
2. Handle URL variations without external dependencies
3. Support idempotent deduplication (applying normalize twice = once)

### Design Constraints

- No new external dependencies (use existing validators, pydantic, stdlib)
- No markdown parsing beyond link extraction (avoid full markdown parser)
- `standardize_github()` is separate utility, not baked into `normalize_url()` yet (allows staged integration)
- Tests must prove idempotence and deduplication contract

## Decisions

### Decision 1: Two-Phase URL Extraction

**Chosen:** Extract markdown links first (precise boundaries), then bare URLs.

**Rationale:**

- Markdown link syntax `[text](url)` has unambiguous boundaries (matched parens)
- Bare URL regex cannot distinguish between URL parens and surrounding text
- Phase 1 removes these URLs from the pool, Phase 2 operates on cleaner text
- Aligns with ai-treadmill's proven approach

**Alternatives Considered:**

- Single-pass regex extraction: Simpler but prone to false positives on balanced bracket edge cases (e.g., Wikipedia URLs with parenthetical disambiguation)
- Only markdown links: Would miss bare URLs in plain text; too restrictive

**Validation:** Test with URLs containing balanced parens: `https://en.wikipedia.org/wiki/Example_(term)` in markdown `[link](https://en.wikipedia.org/wiki/Example_(term))`

---

### Decision 2: www-Stripping in normalize_url()

**Chosen:** Strip `www.` prefix as part of normalization pipeline.

**Rationale:**

- karakeep normalizes `www.example.com` → `example.com`; aizk must match
- `www` is technically a subdomain but functionally equivalent to naked domain
- Enables deduplication across www/non-www variants
- Simple transformation with no semantic loss

**Alternatives Considered:**

- Keep www as-is: Breaks deduplication with karakeep-imported bookmarks
- Separate `strip_www()` function: Adds complexity; should be integrated into main flow

**Validation:** Test that `www.example.com` and `example.com` normalize to identical form

---

### Decision 3: Separate standardize_github() Function

**Chosen:** Keep `standardize_github()` as standalone utility; do not integrate into `normalize_url()` yet.

**Rationale:**

- GitHub URLs have special canonicalization rules (branch stripping, repo root focus)
- Separating concerns allows optional application (some callers may not need it)
- Allows integration into processor pipeline later without coupling
- Defers design decision on whether GitHub canonicalization is always-on or opt-in

**Alternatives Considered:**

- Bake into `normalize_url()`: Couples GitHub logic to general normalization; harder to revisit
- No GitHub support: Leaves deduplication gaps for repo-variant URLs

**Validation:** Test that `raw.githubusercontent.com/owner/repo/main/file.py` and `github.com/owner/repo` normalize (via `standardize_github()`) to same canonical form

---

### Decision 4: extract_domain() Utility

**Chosen:** Simple function to extract domain from validated URL.

**Rationale:**

- Classification logic (is_social_url, detect_source_type) needs reliable domain extraction
- Parsing logic duplicated across callers; centralize it
- Enables type-safe domain extraction with error handling

**Validation:** Test on valid and invalid URLs; ensure it matches domain-only (no path/query)

---

## Architecture

### Module Structure

```text
src/aizk/utilities/url_utils.py

┌─────────────────────────────────────────┐
│ URL Extraction & Normalization Module   │
├─────────────────────────────────────────┤
│ Constants                               │
│  • URL_REGEX                            │
│  • GITHUB_DOMAINS                       │
│  • SOCIAL_MEDIA_DOMAINS (existing)      │
├─────────────────────────────────────────┤
│ Validation (existing)                   │
│  • validate_url(url) → str              │
├─────────────────────────────────────────┤
│ Extraction (IMPROVED)                   │
│  • extract_urls(text) → List[str]       │
│    └─ Two-phase: markdown → bare        │
│  • fix_url_from_markdown(url) → str     │
│  • extract_markdown_urls(text)          │
│    (existing, unchanged)                │
├─────────────────────────────────────────┤
│ Domain Utilities (NEW)                  │
│  • extract_domain(url) → str            │
│  • is_social_url(url) → bool (existing) │
├─────────────────────────────────────────┤
│ Standardization (IMPROVED + NEW)        │
│  • normalize_url(url) → str             │
│    └─ Now strips www                    │
│  • strip_utm_params(url) → str (existing)
│  • safelink_to_url(url) → str (existing)|
│  • standardize_github(url) → str (NEW)  │
└─────────────────────────────────────────┘
```

### Processing Pipeline

```text
Input URL
  ↓
extract_urls() [Phase 1: Markdown links]
  ↓
fix_url_from_markdown() [Remove parsing artifacts]
  ↓
extract_urls() [Phase 2: Bare URLs]
  ↓
validate_url() [Regex + Pydantic check]
  ↓
normalize_url() [Lowercase, strip www, sort params, rm fragment]
  ↓
(optional) standardize_github() [GitHub canonicalization]
  ↓
Output: Canonical URL ready for deduplication
```

## Risks and Mitigations

| Risk                                                                | Mitigation                                                                                                                            |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Idempotence breaks if normalize_url() is not pure                   | **Mitigation:** Test `normalize_url(normalize_url(x)) == normalize_url(x)` for all test cases. No side effects, deterministic output. |
| Two-phase extraction misses URLs in markdown-like text              | **Mitigation:** Test with edge cases (nested brackets, balanced parens in URL). Verify against current regex-only behavior.           |
| GitHub canonicalization removes user intent (branch-specific links) | **Mitigation:** Keep separate utility; document that it reaches repo root. Callers choose whether to apply.                           |
| www-stripping breaks URLs where www is semantic (rarely)            | **Mitigation:** www is never semantic; confirm with test data.                                                                        |
| UTM param stripping loses tracking info (intentional)               | **Mitigation:** Document clearly in docstring. This is feature, not bug.                                                              |

## Testing Strategy

**Unit Tests (atomic behavior):**

- Each function has isolated tests
- Idempotence: `normalize_url(normalize_url(x)) == normalize_url(x)`
- Deduplication: URLs with www/no-www/utm/fragment normalize identically
- Domain extraction: valid and invalid URLs
- GitHub canonicalization: branch stripping, repo root, gist, raw.githubusercontent conversion

**Integration Tests (end-to-end):**

- Extract → validate → normalize pipeline on real bookmark URLs from karakeep samples
- Confirm deduplication semantics hold across variations

**Property Tests (if time):**

- Normalization preserves domain and path semantics
- No information loss on non-tracking-param query string

## Future Considerations (Out of Scope)

- Integration of `standardize_github()` into `normalize_url()` or processor pipeline
- Social media domain classification (deferred decision)
- Full processor pipeline à la ai-treadmill (too much scope)
- Caching normalized URLs for performance
