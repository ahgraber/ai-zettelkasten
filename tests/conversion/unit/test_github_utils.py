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
    standardize_github,
)


class TestStandardizeGithub:
    @pytest.mark.parametrize(
        "input_url,expected",
        [
            # Non-GitHub URLs should remain unchanged
            ("https://example.com/path", "https://example.com/path"),
            # GitHub main site URLs
            ("https://github.com/owner/repo", "https://github.com/owner/repo"),
            # Various branches
            (
                "https://github.com/owner/repo/tree/main",
                "https://github.com/owner/repo/tree/main",
            ),
            (
                "https://github.com/owner/repo/tree/feature-1234",
                "https://github.com/owner/repo/tree/feature-1234",
            ),
            (
                "https://github.com/owner/repo/tree/feature/item-1234",
                "https://github.com/owner/repo/tree/feature/item-1234",
            ),
            (
                "https://github.com/owner/repo/tree/v1.2.34",
                "https://github.com/owner/repo/tree/v1.2.34",
            ),
            # Specific files
            (
                "https://github.com/owner/repo/blob/main/file.py",
                "https://github.com/owner/repo/tree/main/file.py",
            ),
            (
                "https://github.com/owner/repo/blob/feature-1234/file.py",
                "https://github.com/owner/repo/tree/feature-1234/file.py",
            ),
            # Raw URLs should convert to github.com
            (
                "https://raw.githubusercontent.com/owner/repo/refs/heads/main/README.md",
                "https://github.com/owner/repo/tree/main/README.md",
            ),
            (
                "https://raw.githubusercontent.com/owner/repo/refs/heads/main/file.py",
                "https://github.com/owner/repo/tree/main/file.py",
            ),
            (
                "https://raw.githubusercontent.com/owner/repo/refs/heads/master/path/file.txt",
                "https://github.com/owner/repo/tree/master/path/file.txt",
            ),
            # Gist URLs
            ("https://gist.github.com/owner/12345", "https://gist.github.com/owner/12345"),
            # Edge cases
            ("", ""),  # Empty URL
            ("https://github.com", "https://github.com"),  # No path
            ("https://github.com/owner/repo/main", "https://github.com/owner/repo"),  # false branch
            ("https://github.com/invalid@user/repo", "https://github.com/invalid@user/repo"),  # Invalid characters
        ],
    )
    def test_standardize_github(self, input_url: str, expected: str):
        assert standardize_github(input_url) == expected

    def test_different_schemes(self):
        assert standardize_github("http://github.com/owner/repo") == "http://github.com/owner/repo"
        assert standardize_github("git://github.com/owner/repo") == "git://github.com/owner/repo"

    def test_with_query_params(self):
        input_url = "https://github.com/owner/repo?ref=main"
        expected = "https://github.com/owner/repo"
        assert standardize_github(input_url) == expected

    def test_with_fragments(self):
        input_url = "https://github.com/owner/repo#readme"
        expected = "https://github.com/owner/repo"
        assert standardize_github(input_url) == expected

    def test_malformed_urls(self):
        malformed_urls = [
            "not_a_url",
            "github.com/no/scheme",
            "https://github.com/only-owner",
        ]
        for url in malformed_urls:
            assert standardize_github(url) == url

    @pytest.mark.parametrize(
        "input_url",
        [
            "https://github.com/owner/repo/refs/heads/feature",
            "https://raw.githubusercontent.com/owner/repo/refs/heads/feature/file.txt",
            "https://gist.github.com/owner/repo/refs/heads/feature",
        ],
    )
    def test_refs_heads_urls(self, input_url):
        result = standardize_github(input_url)
        assert "refs/heads" not in result
        assert "/owner/repo" in result


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
