# URL Utilities Specification (Delta)

> This is a delta spec for the `url-extraction-normalization` change.
> ADDED: All behavior is new — no baseline `url-utils` spec exists.

## Purpose

Provide robust URL extraction, validation, and normalization for the ai-zettelkasten knowledge graph, ensuring idempotent deduplication and alignment with karakeep's URL semantics without runtime dependency on karakeep.

## Requirements

### Requirement: Extract URLs from text with robust boundary detection

The system SHALL extract both markdown links and bare URLs from text using a two-phase approach that minimizes false positives.

#### ADDED: Scenario: Extract markdown links with precise boundaries

- **GIVEN** text containing markdown links `[text](url)`
- **WHEN** `extract_urls()` is called on the text
- **THEN** the URLs are extracted first, before bare URL extraction, ensuring unambiguous boundaries

#### ADDED: Scenario: Extract bare URLs outside markdown links

- **GIVEN** text containing bare URLs not in markdown syntax
- **WHEN** `extract_urls()` is called on the text
- **THEN** bare URLs are extracted from regions not already captured in Phase 1, reducing boundary ambiguity

#### ADDED: Scenario: Clean up URLs from markdown parsing artifacts

- **GIVEN** a URL with dangling parentheses or punctuation (e.g., from markdown parsing)
- **WHEN** `fix_url_from_markdown()` is applied
- **THEN** trailing non-URL characters are removed while preserving balanced brackets

---

### Requirement: Normalize URLs for deduplication

The system SHALL normalize URLs such that equivalent URLs (same domain and path, different formatting) produce identical normalized strings, enabling reliable deduplication.

#### ADDED: Scenario: Normalize URL is idempotent

- **GIVEN** a valid URL
- **WHEN** `normalize_url(url)` is called twice
- **THEN** both calls return the same normalized string

#### ADDED: Scenario: Strip www prefix for deduplication

- **GIVEN** two URLs that differ only by `www.` prefix (`www.example.com` vs `example.com`)
- **WHEN** both are passed through `normalize_url()`
- **THEN** both return the same normalized string without `www.`

#### ADDED: Scenario: Sort query parameters for consistency

- **GIVEN** two URLs with identical query parameters in different order
- **WHEN** both are passed through `normalize_url()`
- **THEN** both return the same normalized string with sorted parameters

#### ADDED: Scenario: Strip UTM tracking parameters

- **GIVEN** a URL with UTM tracking parameters (`utm_source`, `utm_medium`, etc.)
- **WHEN** `normalize_url()` is called
- **THEN** the URL is normalized with UTM parameters removed

#### ADDED: Scenario: Remove fragment for consistency

- **GIVEN** two URLs that differ only by URL fragment
- **WHEN** both are passed through `normalize_url()`
- **THEN** both return the same normalized string without fragment

---

### Requirement: Extract domain from URL

The system SHALL provide a utility function to extract the domain portion of a URL for classification and deduplication logic.

#### ADDED: Scenario: Extract domain from valid URL

- **GIVEN** a valid URL like `https://github.com/owner/repo`
- **WHEN** `extract_domain(url)` is called
- **THEN** the domain `github.com` is returned

#### ADDED: Scenario: Reject invalid or malformed URLs

- **GIVEN** an invalid URL with no domain
- **WHEN** `extract_domain(url)` is called
- **THEN** a `ValueError` is raised

---

### Requirement: Standardize GitHub URLs

The system SHALL canonicalize GitHub URLs so that multiple URL formats pointing to the same resource normalize to the same canonical form.

#### ADDED: Scenario: Canonicalize raw.githubusercontent.com URLs

- **GIVEN** a GitHub URL from `raw.githubusercontent.com`
- **WHEN** `standardize_github(url)` is called
- **THEN** the URL is converted to canonical `github.com` form

#### ADDED: Scenario: Normalize GitHub repository URLs to root

- **GIVEN** a GitHub URL with branch, blob, or tree path info
- **WHEN** `standardize_github(url)` is called
- **THEN** the URL is normalized to the repository root (owner/repo only)

#### ADDED: Scenario: Canonicalize GitHub gist URLs

- **GIVEN** a GitHub gist URL
- **WHEN** `standardize_github(url)` is called
- **THEN** the URL is normalized to canonical gist form

#### ADDED: Scenario: Pass through non-GitHub URLs unchanged

- **GIVEN** a URL not from a GitHub domain
- **WHEN** `standardize_github(url)` is called
- **THEN** the original URL is returned unchanged

---

### Requirement: Validate URLs before processing

The system SHALL validate URLs against regex pattern and Pydantic's `HttpUrl` type, rejecting malformed or empty URLs early.

#### ADDED: Scenario: Valid URL passes validation

- **GIVEN** a well-formed HTTP or HTTPS URL
- **WHEN** `validate_url(url)` is called
- **THEN** the validated URL string is returned

#### ADDED: Scenario: Empty URL is rejected

- **GIVEN** an empty or whitespace-only string
- **WHEN** `validate_url(url)` is called
- **THEN** a `ValueError` is raised

#### ADDED: Scenario: Malformed URL is rejected

- **GIVEN** a string that does not match the URL regex pattern
- **WHEN** `validate_url(url)` is called
- **THEN** a `ValueError` is raised with a descriptive message

---

## Technical Notes

- **Implementation File:** `src/aizk/utilities/url_utils.py`
- **Test Files:** `tests/conversion/unit/test_url_utils.py`, `tests/utilities/test_url_utils.py`
- **Dependencies:** `urllib.parse`, `re`, `validators`, `pydantic.HttpUrl`, existing `check_balanced_brackets` and `temp_env_var` utilities
- **Deduplication Contract:** Two URLs normalize to the same string if and only if they represent the same logical resource (domain, path, query params) regardless of www prefix, UTM params, or fragment.
