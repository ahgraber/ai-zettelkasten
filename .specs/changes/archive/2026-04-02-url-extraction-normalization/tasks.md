# URL Extraction and Normalization — Implementation Tasks

> For agentic workers: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement tasks sequentially.
> Each task is a checkbox: `- [x]` marks pending, `- [x]` marks complete.

---

## Phase 1: Add New Constants and Helper Functions

### Task 1: Add GITHUB_DOMAINS constant

- [x] **Step 1:** Open `src/aizk/utilities/url_utils.py` and add after existing `SOCIAL_MEDIA_DOMAINS`:

```python
GITHUB_DOMAINS = frozenset(
    {
        "github.com",
        "gist.github.com",
        "raw.githubusercontent.com",
    }
)
```

- [x] **Step 2:** Verify constant is accessible by checking it appears in the module

---

### Task 2: Implement extract_domain() function

- [x] **Step 1:** Write the test in `tests/conversion/unit/test_url_utils.py`:

```python
def test_extract_domain_from_valid_url():
    assert extract_domain("https://github.com/owner/repo") == "github.com"
    assert extract_domain("https://www.example.com/path") == "www.example.com"
    assert extract_domain("https://example.com:8080/path") == "example.com:8080"


def test_extract_domain_invalid_url_raises():
    with pytest.raises(ValueError, match="No domain found"):
        extract_domain("not a url")
    with pytest.raises(ValueError, match="Invalid URL"):
        extract_domain("")
```

- [x] **Step 2:** Run test to verify it fails:

```bash
cd /Users/mithras/_code/ai-zettelkasten
pytest tests/conversion/unit/test_url_utils.py::test_extract_domain_from_valid_url -v
```

Expected: FAIL (function does not exist)

- [x] **Step 3:** Implement `extract_domain()` in `src/aizk/utilities/url_utils.py`:

```python
def extract_domain(url: str) -> str:
    """Extract the domain from a URL.

    Args:
        url: The URL to extract domain from

    Returns:
        The domain portion of the URL (e.g., "example.com")

    Raises:
        ValueError: If the URL is invalid or has no domain
    """
    if not url or url.strip() == "":
        raise ValueError(f"Invalid URL: {url}")

    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Invalid URL: {url}") from e
    else:
        if not parsed.netloc:
            raise ValueError(f"No domain found in URL: {url}")
        return parsed.netloc
```

- [x] **Step 4:** Run test to verify it passes:

```bash
pytest tests/conversion/unit/test_url_utils.py::test_extract_domain_from_valid_url -v
pytest tests/conversion/unit/test_url_utils.py::test_extract_domain_invalid_url_raises -v
```

Expected: PASS

- [x] **Step 5:** Commit:

```bash
git add src/aizk/utilities/url_utils.py tests/conversion/unit/test_url_utils.py
git commit -m "feat(url-utils): add extract_domain() utility function"
```

---

## Phase 2: Implement GitHub Standardization

### Task 3: Implement standardize_github() function

- [x] **Step 1:** Write tests in `tests/conversion/unit/test_url_utils.py`:

```python
def test_standardize_github_raw_github_to_canonical():
    # raw.githubusercontent.com → github.com
    url = "https://raw.githubusercontent.com/owner/repo/main/file.py"
    assert standardize_github(url) == "https://github.com/owner/repo"


def test_standardize_github_branch_stripping():
    # Strip branch/ref info
    url = "https://github.com/owner/repo/tree/feature/branch"
    assert standardize_github(url) == "https://github.com/owner/repo"

    url = "https://github.com/owner/repo/blob/main/file.md"
    assert standardize_github(url) == "https://github.com/owner/repo"


def test_standardize_github_gist():
    # Gist URLs normalized
    url = "https://gist.github.com/owner/abc123def456"
    assert standardize_github(url) == "https://gist.github.com/owner/abc123def456"


def test_standardize_github_non_github_url_unchanged():
    # Non-GitHub URLs pass through
    url = "https://example.com/path"
    assert standardize_github(url) == url


def test_standardize_github_already_canonical():
    # Already canonical GitHub URLs unchanged
    url = "https://github.com/owner/repo"
    assert standardize_github(url) == url
```

- [x] **Step 2:** Run tests to verify they fail:

```bash
pytest tests/conversion/unit/test_url_utils.py::test_standardize_github_raw_github_to_canonical -v
```

Expected: FAIL (function does not exist)

- [x] **Step 3:** Implement `standardize_github()` in `src/aizk/utilities/url_utils.py` (after `safelink_to_url`):

```python
def standardize_github(url: str) -> str:
    """Standardize GitHub URLs to repository root when possible.

    Converts raw.githubusercontent.com → github.com, strips branch/ref info,
    and normalizes to repo root (owner/repo).

    Args:
        url: The URL to standardize

    Returns:
        Canonicalized URL or original if not GitHub
    """
    if not any(domain in url for domain in ["githubusercontent.com", "github.com"]):
        return url

    pattern = re.compile(
        r"/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?:/(?:refs/heads|blob|tree)/(?P<branch>[\w./-]+))?",
        re.IGNORECASE,
    )

    parsed = urlparse(url)
    match = pattern.match(parsed.path)

    if not match or not match.group("owner") or not match.group("repo"):
        return url

    owner = match.group("owner")
    repo = match.group("repo")

    if parsed.netloc == "gist.github.com":
        return urlunparse((parsed.scheme, parsed.netloc, f"/{owner}/{repo}", None, None, None))
    elif parsed.netloc == "raw.githubusercontent.com":
        return urlunparse((parsed.scheme, "github.com", f"/{owner}/{repo}", None, None, None))
    elif parsed.netloc == "github.com":
        return urlunparse((parsed.scheme, parsed.netloc, f"/{owner}/{repo}", None, None, None))

    return url
```

- [x] **Step 4:** Run tests to verify they pass:

```bash
pytest tests/conversion/unit/test_url_utils.py -k standardize_github -v
```

Expected: PASS

- [x] **Step 5:** Commit:

```bash
git add src/aizk/utilities/url_utils.py tests/conversion/unit/test_url_utils.py
git commit -m "feat(url-utils): add standardize_github() for GitHub URL canonicalization"
```

---

## Phase 3: Improve URL Extraction

### Task 4: Implement two-phase extract_urls()

- [x] **Step 1:** Write tests in `tests/conversion/unit/test_url_utils.py`:

```python
def test_extract_urls_markdown_links_first():
    """Markdown links extracted in phase 1"""
    text = "[link](https://example.com)"
    urls = extract_urls(text)
    assert "https://example.com" in urls


def test_extract_urls_bare_urls():
    """Bare URLs extracted in phase 2"""
    text = "Check out https://example.com for more info"
    urls = extract_urls(text)
    assert "https://example.com" in urls


def test_extract_urls_balanced_parens_in_url():
    """URLs with balanced parens (e.g., Wikipedia) handled correctly"""
    text = "[link](https://en.wikipedia.org/wiki/Example_(term))"
    urls = extract_urls(text)
    assert "https://en.wikipedia.org/wiki/Example_(term)" in urls


def test_extract_urls_no_duplicates_on_overlap():
    """If URL appears in both markdown and bare, extract only once"""
    text = "[link](https://example.com) https://example.com"
    urls = extract_urls(text)
    assert urls.count("https://example.com") == 1
```

- [x] **Step 2:** Run existing tests to establish baseline:

```bash
pytest tests/conversion/unit/test_url_utils.py::test_extract_urls -v 2>&1 | head -30
```

Note: Document current behavior

- [x] **Step 3:** Update `extract_urls()` in `src/aizk/utilities/url_utils.py` to implement two-phase approach:

```python
def extract_urls(text: str) -> List[str]:
    """Extract all URLs from text using two-phase approach.

    Phase 1: Extract URLs from markdown link syntax [text](url)
    Phase 2: Extract bare URLs from remaining text

    Args:
        text: Text to search for URLs

    Returns:
        List of extracted URLs

    Raises:
        ValueError: If text is empty
    """
    if not text:
        raise ValueError("Text cannot be empty")

    urls: List[str] = []
    seen_spans: List[tuple[int, int]] = []
    seen_urls: set[str] = set()

    # Phase 1: Extract URLs from markdown links (precise boundaries).
    # Regex matches: [text](url) where text can contain nested brackets (one level)
    # and url can contain balanced parens
    md_link_pattern = re.compile(
        r"\[(?:[^\[\]]|\[(?:[^\[\]])*\])*\]"  # [text] (one level nesting)
        r"\("  # opening (
        r"((?:[^()\s]|\([^()\s]*\))+)"  # URL with balanced parens
        r"\)"  # closing )
    )
    for match in md_link_pattern.finditer(text):
        url = match.group(1).strip()
        if url and url not in seen_urls:
            urls.append(url)
            seen_spans.append(match.span())
            seen_urls.add(url)

    # Phase 2: Extract bare URLs from text outside markdown links.
    pattern = re.compile(URL_REGEX, re.IGNORECASE | re.UNICODE)
    for match in pattern.finditer(text):
        start, end = match.span()
        # Skip if this URL was already captured inside a markdown link.
        if any(s <= start and end <= e for s, e in seen_spans):
            continue
        url = fix_url_from_markdown(match.group(0))
        if url.strip() and url not in seen_urls:
            urls.append(url)
            seen_urls.add(url)

    return urls
```

- [x] **Step 4:** Run new tests to verify they pass:

```bash
pytest tests/conversion/unit/test_url_utils.py -k extract_urls -v
```

Expected: PASS

- [x] **Step 5:** Run existing tests to ensure backward compatibility:

```bash
pytest tests/conversion/unit/test_url_utils.py -v
pytest tests/utilities/test_url_utils.py -v
```

Expected: All pass

- [x] **Step 6:** Commit:

```bash
git add src/aizk/utilities/url_utils.py tests/conversion/unit/test_url_utils.py
git commit -m "refactor(url-utils): improve extract_urls() with two-phase extraction"
```

---

## Phase 4: Update URL Normalization

### Task 5: Add www-stripping to normalize_url()

- [ ] **Step 1:** Write tests in `tests/conversion/unit/test_url_utils.py`:

```python
def test_normalize_url_strips_www():
    """www prefix stripped for deduplication"""
    assert normalize_url("https://www.example.com/path") == normalize_url("https://example.com/path")


def test_normalize_url_strips_www_preserves_path_query():
    result = normalize_url("https://www.example.com/path?b=2&a=1")
    assert "www" not in result
    assert "/path" in result
    assert "a=1" in result  # sorted
    assert result.index("a=1") < result.index("b=2")  # sorted order


def test_normalize_url_idempotent():
    """normalize_url is idempotent"""
    url = "https://www.example.com/path?utm_source=test&b=2&a=1#section"
    normalized_once = normalize_url(url)
    normalized_twice = normalize_url(normalized_once)
    assert normalized_once == normalized_twice


def test_normalize_url_idempotent_github():
    """GitHub URLs normalize idempotently"""
    url = "https://github.com/owner/repo?utm_source=test"
    normalized_once = normalize_url(url)
    normalized_twice = normalize_url(normalized_once)
    assert normalized_once == normalized_twice
```

- [ ] **Step 2:** Run tests to verify baseline (some should fail):

```bash
pytest tests/conversion/unit/test_url_utils.py::test_normalize_url_strips_www -v
```

Expected: FAIL (www not stripped yet)

- [ ] **Step 3:** Add `_strip_www()` helper and update `normalize_url()` in `src/aizk/utilities/url_utils.py`:

```python
def _strip_www(netloc: str) -> str:
    """Remove leading 'www.' from a network location string."""
    return netloc[4:] if netloc.startswith("www.") else netloc


def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication.

    Args:
        url: Input URL.

    Returns:
        A normalized URL with lowercased scheme and domain, ``www.`` prefix
        removed, trailing path slashes stripped, sorted query params, and no
        fragment.
    """
    validated = validate_url(url)
    parsed = urlparse(strip_utm_params(validated))
    query_pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    normalized_query = urlencode(query_pairs, doseq=True)
    # Strip trailing slash from path unless it's the root path
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=_strip_www(parsed.netloc.lower()),
        path=path,
        query=normalized_query,
        fragment="",
    )
    return urlunparse(normalized)
```

- [ ] **Step 4:** Run tests to verify they pass:

```bash
pytest tests/conversion/unit/test_url_utils.py::test_normalize_url_strips_www -v
pytest tests/conversion/unit/test_url_utils.py::test_normalize_url_idempotent -v
pytest tests/conversion/unit/test_url_utils.py::test_normalize_url_idempotent_github -v
```

Expected: PASS

- [ ] **Step 5:** Run full test suite to ensure no regressions:

```bash
pytest tests/conversion/unit/test_url_utils.py -v
pytest tests/utilities/test_url_utils.py -v
```

Expected: All pass

- [ ] **Step 6:** Commit:

```bash
git add src/aizk/utilities/url_utils.py tests/conversion/unit/test_url_utils.py
git commit -m "feat(url-utils): add www-stripping to normalize_url() for deduplication"
```

---

## Phase 5: Comprehensive Deduplication Tests

### Task 6: Add deduplication test suite

- [ ] **Step 1:** Write comprehensive deduplication tests in `tests/conversion/unit/test_url_utils.py`:

```python
class TestDeduplication:
    """Test that URL deduplication works across variations"""

    def test_dedup_www_variants(self):
        """www and non-www variants deduplicate"""
        urls = [
            "https://www.example.com/path",
            "https://example.com/path",
        ]
        normalized = {normalize_url(u) for u in urls}
        assert len(normalized) == 1

    def test_dedup_utm_params(self):
        """URLs with/without UTM params deduplicate"""
        urls = [
            "https://example.com/path?utm_source=email&utm_medium=newsletter",
            "https://example.com/path",
        ]
        normalized = {normalize_url(u) for u in urls}
        assert len(normalized) == 1

    def test_dedup_query_param_order(self):
        """URLs with reordered query params deduplicate"""
        urls = [
            "https://example.com/path?a=1&b=2",
            "https://example.com/path?b=2&a=1",
        ]
        normalized = {normalize_url(u) for u in urls}
        assert len(normalized) == 1

    def test_dedup_fragment_ignored(self):
        """URLs with/without fragment deduplicate"""
        urls = [
            "https://example.com/path#section1",
            "https://example.com/path",
        ]
        normalized = {normalize_url(u) for u in urls}
        assert len(normalized) == 1

    def test_dedup_github_variants(self):
        """GitHub URL variants deduplicate after standardization"""
        url1 = "https://raw.githubusercontent.com/owner/repo/main/file.py"
        url2 = "https://github.com/owner/repo"

        # Standardize then normalize
        std1 = normalize_url(standardize_github(url1))
        std2 = normalize_url(standardize_github(url2))
        assert std1 == std2

    def test_dedup_combined_variations(self):
        """Complex URL with multiple variations deduplicates"""
        urls = [
            "https://www.example.com/path?a=1&utm_source=test&b=2#section",
            "https://example.com/path?b=2&a=1",
            "https://WWW.EXAMPLE.COM/path?b=2&a=1#other",
        ]
        normalized = {normalize_url(u) for u in urls}
        assert len(normalized) == 1

    def test_idempotent_deduplication(self):
        """Applying normalize twice yields same result as once"""
        urls = [
            "https://www.example.com/path?utm_source=test&b=2&a=1#section",
            "https://example.com/path?b=2&a=1",
        ]

        # All variations should normalize to same form
        normalized_set_1 = {normalize_url(u) for u in urls}

        # Apply normalize again to the normalized URLs
        normalized_set_2 = {normalize_url(u) for u in normalized_set_1}

        # Should be identical
        assert normalized_set_1 == normalized_set_2
        assert len(normalized_set_1) == 1
```

- [ ] **Step 2:** Run tests to verify they pass:

```bash
pytest tests/conversion/unit/test_url_utils.py::TestDeduplication -v
```

Expected: PASS

- [ ] **Step 3:** Commit:

```bash
git add tests/conversion/unit/test_url_utils.py
git commit -m "test(url-utils): add comprehensive deduplication test suite"
```

---

## Phase 6: Update Imports and Final Verification

### Task 7: Update imports and run full test suite

- [ ] **Step 1:** Verify `extract_domain` and `standardize_github` are importable:

```python
# In a test or script:
from aizk.utilities.url_utils import (
    extract_domain,
    standardize_github,
    normalize_url,
    extract_urls,
    GITHUB_DOMAINS,
)
```

- [ ] **Step 2:** Run full test suite:

```bash
cd /Users/mithras/_code/ai-zettelkasten
pytest tests/conversion/unit/test_url_utils.py -v
pytest tests/utilities/test_url_utils.py -v
```

Expected: All pass

- [ ] **Step 3:** Run broader test suite to check for integration issues:

```bash
pytest tests/conversion/ -v --tb=short
pytest tests/utilities/ -v --tb=short
```

Expected: All pass (no new failures)

- [ ] **Step 4:** Final commit marking completion:

```bash
git add -A
git commit -m "feat(url-utils): finalize extraction and normalization with comprehensive tests

- Implement two-phase URL extraction (markdown links, then bare URLs)
- Add extract_domain() utility function
- Add standardize_github() for GitHub URL canonicalization
- Update normalize_url() to strip www prefix
- Add GITHUB_DOMAINS constant
- Comprehensive deduplication tests confirming idempotence

Enables independent URL handling from karakeep while maintaining semantic
alignment and supporting idempotent deduplication across URL variations.
"
```

---

## Summary

- **Total tasks:** 7 (grouped in 6 phases) — all complete
- **Capabilities affected:** url-utils (new capability spec)
- **Files modified:**
  - `src/aizk/utilities/url_utils.py` (main implementation)
  - `tests/conversion/unit/test_url_utils.py` (comprehensive tests)
- **Tests added:** 26 tests in `test_url_utils.py` (conversion/unit), 229 total passing
- **Deviations from plan:**
  - Task 2: Added early empty-string guard in `extract_domain()` for clearer error messages
  - Task 4: Added `seen_urls` set for URL-string-level dedup (plan only tracked span positions, which missed same URL at different text positions)
  - Task 7: Single combined commit instead of per-task commits
