"""GitHub URL helpers for conversion workflows."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from aizk.utilities.url_utils import validate_url

GITHUB_DOMAINS = frozenset({"github.com", "gist.github.com", "raw.githubusercontent.com"})


def is_github_url(url: str) -> bool:
    """Check if URL is from a GitHub domain.

    Args:
        url: URL string to inspect.

    Returns:
        True when the URL belongs to a GitHub domain.
    """
    validated = validate_url(url)

    try:
        parsed = urlparse(str(validated))
    except Exception:
        return False
    else:
        netloc = parsed.netloc.lower()
        return netloc in GITHUB_DOMAINS or any(netloc.endswith("." + domain) for domain in GITHUB_DOMAINS)


def standardize_github(url: str) -> str:
    """Standardize GitHub URLs to repository root when possible.

    Args:
        url: URL string to normalize.

    Returns:
        Normalized GitHub repository URL or the original URL if not applicable.
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
    branch = match.group("branch")

    if parsed.netloc == "gist.github.com":
        return urlunparse((parsed.scheme, parsed.netloc, f"{owner}/{repo}", None, None, None))
    if parsed.netloc == "raw.githubusercontent.com":
        path = f"{owner}/{repo}"
        if branch:
            path += f"/tree/{branch}"
        return urlunparse((parsed.scheme, "github.com", path, None, None, None))
    if parsed.netloc == "github.com":
        path = f"{owner}/{repo}"
        if branch:
            path += f"/tree/{branch}"
        return urlunparse((parsed.scheme, parsed.netloc, path, None, None, None))

    return url


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
    std_url = standardize_github(source_url)
    if not std_url.startswith("https://github.com/"):
        raise ValueError(f"Invalid GitHub URL: {source_url}")

    parts = std_url.rstrip("/").split("/")
    if len(parts) < 5:
        raise ValueError(f"Cannot parse owner/repo from URL: {source_url}")
    return parts[3], parts[4]


def is_github_pages_url(source_url: str) -> bool:
    """Return True when the URL points at a GitHub Pages site.

    Args:
        source_url: URL to inspect.

    Returns:
        True when the hostname ends with github.io.
    """
    parsed = urlparse(source_url)
    return parsed.netloc.lower().endswith("github.io")
