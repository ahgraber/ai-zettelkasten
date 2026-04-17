"""GitHub URL helpers for conversion workflows."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from aizk.utilities.url_utils import _netloc_in_domains

GITHUB_DOMAINS = frozenset({"github.com", "gist.github.com", "raw.githubusercontent.com"})

_GITHUB_PATH_PATTERN = re.compile(
    r"/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?:/(?:refs/heads|blob|tree)/(?P<branch>[\w./-]+))?",
    re.IGNORECASE,
)


def standardize_github_to_repo(url: str) -> str:
    """Standardize GitHub URLs to repository root for deduplication.

    Converts raw.githubusercontent.com → github.com and strips all path
    segments beyond owner/repo (branches, files, issues, PRs, etc.).
    This function is intentionally lossy — it exists to group all URLs
    referencing the same repository.

    Args:
        url: URL string to normalize.

    Returns:
        Repository-root URL (``https://github.com/owner/repo``) or the
        original URL if not a recognised GitHub domain.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc not in GITHUB_DOMAINS and not any(netloc.endswith("." + d) for d in GITHUB_DOMAINS):
        return url

    match = _GITHUB_PATH_PATTERN.match(parsed.path)
    if not match or not match.group("owner") or not match.group("repo"):
        return url

    owner = match.group("owner")
    repo = match.group("repo")
    target_netloc = "github.com" if parsed.netloc == "raw.githubusercontent.com" else parsed.netloc
    return urlunparse((parsed.scheme, target_netloc, f"/{owner}/{repo}", "", "", ""))


def is_github_url(url: str) -> bool:
    """Check if URL is from a GitHub domain.

    Args:
        url: URL string to inspect.

    Returns:
        True when the URL belongs to a GitHub domain.
    """
    return _netloc_in_domains(url, GITHUB_DOMAINS)


def is_github_repo_root(source_url: str) -> bool:
    """Return True when the URL points at a GitHub repository root.

    Args:
        source_url: GitHub URL to inspect.

    Returns:
        True when the URL path is /owner/repo.
    """
    parsed = urlparse(source_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    return len(path_parts) == 2


def source_mentions_readme(source_url: str) -> bool:
    """Return True when the URL path includes 'readme'.

    Args:
        source_url: GitHub URL to inspect.

    Returns:
        True when "readme" appears in the URL.
    """
    return "readme" in source_url.lower()


def parse_github_owner_repo(source_url: str) -> tuple[str, str]:
    """Extract GitHub owner/repo from a URL.

    Args:
        source_url: GitHub URL to parse.

    Returns:
        Tuple of (owner, repo).

    Raises:
        ValueError: If the URL cannot be parsed into owner/repo.
    """
    std_url = standardize_github_to_repo(source_url)
    if not std_url.startswith("https://github.com/"):
        raise ValueError(f"Invalid GitHub URL: {source_url}")

    parts = [p for p in urlparse(std_url).path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Cannot parse owner/repo from URL: {source_url}")
    return parts[0], parts[1]


def is_github_pages_url(source_url: str) -> bool:
    """Return True when the URL points at a GitHub Pages site.

    Args:
        source_url: URL to inspect.

    Returns:
        True when the hostname ends with github.io.
    """
    parsed = urlparse(source_url)
    return parsed.netloc.lower().endswith("github.io")


def __getattr__(name: str) -> object:
    if name == "GithubReadmeFetcher":
        from aizk.conversion.adapters.fetchers.github import GithubReadmeFetcher
        return GithubReadmeFetcher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
