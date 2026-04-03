"""Unit tests for conversion URL utilities."""

import pytest

from aizk.conversion.utilities.arxiv_utils import get_arxiv_id
from aizk.conversion.utilities.bookmark_utils import detect_source_type
from aizk.utilities.url_utils import extract_domain, extract_urls, normalize_url, standardize_github_to_repo


def test_normalize_url_sorts_query_and_drops_fragment():
    url = "https://Example.com/path?b=2&a=1#section"
    assert normalize_url(url) == "https://example.com/path?a=1&b=2"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arxiv.org/abs/1706.03762", "arxiv"),
        ("https://github.com/org/repo", "github"),
        ("https://example.com/file", "other"),
    ],
)
def test_detect_source_type(url, expected):
    assert detect_source_type(url) == expected


def test_extract_domain_from_valid_url():
    assert extract_domain("https://github.com/owner/repo") == "github.com"
    assert extract_domain("https://www.example.com/path") == "www.example.com"
    assert extract_domain("https://example.com:8080/path") == "example.com:8080"


def test_extract_domain_invalid_url_raises():
    with pytest.raises(ValueError):
        extract_domain("not a url")
    with pytest.raises(ValueError):
        extract_domain("")


def test_extract_domain_rejects_malformed_urls():
    """Malformed URLs with spaces or invalid ports must be rejected."""
    with pytest.raises((ValueError, Exception)):
        extract_domain("https://exa mple.com/path")
    with pytest.raises((ValueError, Exception)):
        extract_domain("https://github.com:bad/path")


def test_standardize_github_raw_github_to_canonical():
    # raw.githubusercontent.com → github.com
    url = "https://raw.githubusercontent.com/owner/repo/main/file.py"
    assert standardize_github_to_repo(url) == "https://github.com/owner/repo"


def test_standardize_github_branch_stripping():
    # Strip branch/ref info
    url = "https://github.com/owner/repo/tree/feature/branch"
    assert standardize_github_to_repo(url) == "https://github.com/owner/repo"

    url = "https://github.com/owner/repo/blob/main/file.md"
    assert standardize_github_to_repo(url) == "https://github.com/owner/repo"


def test_standardize_github_gist():
    # Gist URLs normalized
    url = "https://gist.github.com/owner/abc123def456"
    assert standardize_github_to_repo(url) == "https://gist.github.com/owner/abc123def456"


def test_standardize_github_non_github_url_unchanged():
    # Non-GitHub URLs pass through
    url = "https://example.com/path"
    assert standardize_github_to_repo(url) == url


def test_standardize_github_already_canonical():
    # Already canonical GitHub URLs unchanged
    url = "https://github.com/owner/repo"
    assert standardize_github_to_repo(url) == url


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


def test_normalize_url_strips_www():
    """www prefix stripped for deduplication"""
    assert normalize_url("https://www.example.com/path") == normalize_url("https://example.com/path")


def test_normalize_url_strips_www_preserves_path_query():
    result = normalize_url("https://www.example.com/path?b=2&a=1")
    assert "www" not in result
    assert "/path" in result
    assert "a=1" in result
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


class TestDeduplication:
    """Confirm URL deduplication is correct and idempotent across variations."""

    def test_dedup_www_variants(self):
        urls = [
            "https://www.example.com/path",
            "https://example.com/path",
        ]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_dedup_utm_params(self):
        urls = [
            "https://example.com/path?utm_source=email&utm_medium=newsletter",
            "https://example.com/path",
        ]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_dedup_query_param_order(self):
        urls = [
            "https://example.com/path?a=1&b=2",
            "https://example.com/path?b=2&a=1",
        ]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_dedup_fragment_ignored(self):
        urls = [
            "https://example.com/path#section1",
            "https://example.com/path",
        ]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_dedup_github_variants(self):
        """GitHub URL variants deduplicate after standardization."""
        url1 = "https://raw.githubusercontent.com/owner/repo/main/file.py"
        url2 = "https://github.com/owner/repo"
        assert normalize_url(standardize_github_to_repo(url1)) == normalize_url(standardize_github_to_repo(url2))

    def test_dedup_combined_variations(self):
        """Complex URL with multiple simultaneous variations deduplicates."""
        urls = [
            "https://www.example.com/path?a=1&utm_source=test&b=2#section",
            "https://example.com/path?b=2&a=1",
            "https://WWW.EXAMPLE.COM/path?b=2&a=1#other",
        ]
        assert len({normalize_url(u) for u in urls}) == 1

    def test_idempotent_deduplication(self):
        """Applying normalize_url twice produces same result as once."""
        urls = [
            "https://www.example.com/path?utm_source=test&b=2&a=1#section",
            "https://example.com/path?b=2&a=1",
        ]
        normalized_once = {normalize_url(u) for u in urls}
        normalized_twice = {normalize_url(u) for u in normalized_once}
        assert normalized_once == normalized_twice
        assert len(normalized_once) == 1
