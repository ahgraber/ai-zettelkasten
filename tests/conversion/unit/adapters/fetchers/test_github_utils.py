"""Unit tests for GitHub utility functions."""

from pydantic import ValidationError as PydanticValidationError
import pytest
from validators import ValidationError as URLValidatorValidationError

from aizk.conversion.utilities.github_utils import (
    is_github_pages_url,
    is_github_repo_root,
    is_github_url,
    parse_github_owner_repo,
    source_mentions_readme,
    standardize_github_to_repo,
)
from aizk.utilities.url_utils import normalize_url


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


def test_dedup_github_variants():
    """GitHub URL variants deduplicate after standardization."""
    url1 = "https://raw.githubusercontent.com/owner/repo/main/file.py"
    url2 = "https://github.com/owner/repo"
    assert normalize_url(standardize_github_to_repo(url1)) == normalize_url(standardize_github_to_repo(url2))


class TestIsGithubUrl:
    """Test the is_github_url function for detecting GitHub URLs."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/user/repo",
            "https://gist.github.com/user/123",
            "https://raw.githubusercontent.com/user/repo/main/file.txt",
        ],
    )
    def test_github_urls_exact_domain(self, url):
        assert is_github_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.github.com/user/repo",
            "https://api.github.com/repos/user/repo",
            "https://docs.github.com/en/get-started",
            "https://mobile.github.com/user/repo",
            "https://subdomain.gist.github.com/user/123",
        ],
    )
    def test_github_urls_with_subdomains(self, url):
        assert is_github_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/user/repo",
            "https://gitlab.com/user/repo",
            "https://bitbucket.org/user/repo",
            "https://linkedin.com/in/someone",
            "https://github.io/user/repo",
        ],
    )
    def test_non_github_urls(self, url):
        assert is_github_url(url) is False

    def test_invalid_url(self):
        with pytest.raises((PydanticValidationError, URLValidatorValidationError, ValueError)):
            is_github_url("not-a-url")


class TestGithubHelpers:
    def test_is_github_pages_url(self):
        assert is_github_pages_url("https://example.github.io") is True
        assert is_github_pages_url("https://example.github.io/project") is True
        assert is_github_pages_url("https://github.com/example/project") is False

    def test_is_github_repo_root(self):
        assert is_github_repo_root("https://github.com/owner/repo") is True
        assert is_github_repo_root("https://github.com/owner/repo/") is True
        assert is_github_repo_root("https://github.com/owner/repo/blob/main/README.md") is False

    def test_source_mentions_readme(self):
        assert source_mentions_readme("https://github.com/owner/repo/blob/main/README.md") is True
        assert source_mentions_readme("https://github.com/owner/repo/blob/main/readme.rst") is True
        assert source_mentions_readme("https://github.com/owner/repo/blob/main/docs.md") is False

    def test_parse_github_owner_repo(self):
        assert parse_github_owner_repo("https://github.com/owner/repo") == ("owner", "repo")
        assert parse_github_owner_repo("https://raw.githubusercontent.com/owner/repo/refs/heads/main/README.md") == (
            "owner",
            "repo",
        )
        with pytest.raises(ValueError):
            parse_github_owner_repo("https://gitlab.com/owner/repo")
