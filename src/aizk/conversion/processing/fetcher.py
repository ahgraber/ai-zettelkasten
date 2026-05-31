"""Content fetching utilities for conversion workers.

Error classes and low-level HTTP helpers now live in canonical locations:
  - aizk.conversion.core.errors  (FetchError, BookmarkContentUnavailableError, etc.)
  - aizk.conversion.adapters.fetchers._http  (fetch_karakeep_asset, fetch_arxiv_pdf)

Re-exported here for backward compatibility.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from aizk.conversion.core.errors import (
    ArxivPdfFetchError,
    BookmarkContentUnavailableError,
    FetchError,
    GitHubReadmeNotFoundError,
)
from aizk.conversion.utilities.arxiv_utils import get_arxiv_id, is_arxiv_url
from aizk.conversion.utilities.bookmark_utils import (
    get_bookmark_asset_id,
    get_bookmark_html_content,
    get_bookmark_source_url,
    is_pdf_asset,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.fetch_helpers import fetch_arxiv_pdf, fetch_karakeep_asset
from aizk.conversion.utilities.github_utils import (
    is_github_pages_url,
    is_github_repo_root,
    is_github_url,
    parse_github_owner_repo,
    source_mentions_readme,
)
from karakeep_client.models import Bookmark

logger = logging.getLogger(__name__)


def _is_arxiv_abstract_url(url: str) -> bool:
    """Check if URL is an arXiv abstract page."""
    try:
        parsed = urlparse(url)
        return parsed.path.startswith("/abs")
    except Exception:
        return False


async def fetch_arxiv(
    bookmark: Bookmark,
    config: ConversionConfig,
    *,
    asset_bytes: bytes | None = None,
) -> bytes:
    """Fetch arXiv paper from KaraKeep bookmark.

    KaraKeep bookmark is the source of truth. Business logic:
    1. If source URL is arxiv.org/abs (abstract page) → download PDF from arXiv
    2. If bookmark is a PDF asset → use it (or fetch from KaraKeep)
    3. If link bookmark with html content → download PDF from arXiv

    Args:
        bookmark: KaraKeep bookmark object (source of truth).
        config: Conversion configuration.
        asset_bytes: Optional pre-fetched asset bytes for PDF assets.
        karakeep_client: Optional client for fetching assets from KaraKeep.

    Returns:
        PDF content as bytes.

    Raises:
        BookmarkContentUnavailableError: If bookmark has no usable content.
        ArxivPdfFetchError: If PDF fetch from arXiv fails.
        FetchError: If asset fetch from KaraKeep fails.
    """
    source_url = get_bookmark_source_url(bookmark)
    if not is_arxiv_url(source_url):
        raise BookmarkContentUnavailableError(f"Bookmark {bookmark.id} is not an arXiv bookmark: {source_url}")

    arxiv_id = get_arxiv_id(source_url)

    # Case 1: Abstract page bookmark → download PDF from arXiv
    if source_url and _is_arxiv_abstract_url(source_url):
        logger.info("arXiv abstract bookmark %s; fetching PDF for %s", bookmark.id, arxiv_id)
        return await fetch_arxiv_pdf(arxiv_id, config)

    # Case 2: PDF asset bookmark → use asset bytes or fetch from KaraKeep
    if is_pdf_asset(bookmark):
        if asset_bytes:
            logger.info("Using provided PDF asset bytes for arXiv %s", arxiv_id)
            return asset_bytes

        asset_id = get_bookmark_asset_id(bookmark)
        if asset_id:
            logger.info("Fetching PDF asset %s from KaraKeep for arXiv %s", asset_id, arxiv_id)
            return await fetch_karakeep_asset(asset_id)

        raise BookmarkContentUnavailableError(f"Bookmark {bookmark.id} has PDF asset but no way to fetch it")

    # Case 3: Link bookmark with html content → download PDF from arXiv
    html_content = get_bookmark_html_content(bookmark)
    if html_content:
        source_url = get_bookmark_source_url(bookmark)
        if not is_arxiv_url(source_url):
            raise BookmarkContentUnavailableError(f"Bookmark {bookmark.id} html content is not from arXiv source URL")
        arxiv_id = get_arxiv_id(source_url)
        logger.info("Link bookmark %s with html content; fetching PDF for arXiv %s", bookmark.id, arxiv_id)
        return await fetch_arxiv_pdf(arxiv_id, config)

    # No usable content
    raise BookmarkContentUnavailableError(f"Bookmark {bookmark.id} has no usable content")


async def fetch_github_readme(
    bookmark: Bookmark,
    config: ConversionConfig,
    *,
    html_content: str | None = None,
) -> bytes:
    """Fetch GitHub README as HTML.

    Uses KaraKeep HTML when the bookmark is a repo root or README page;
    otherwise fetches the rendered README HTML from GitHub.

    Args:
        bookmark: KaraKeep bookmark object (source of truth).
        config: Conversion configuration.
        html_content: Optional HTML content from KaraKeep for repo/README pages.

    Returns:
        README content as HTML bytes.

    Raises:
        BookmarkContentUnavailableError: If the bookmark is not a GitHub URL.
        GitHubReadmeNotFoundError: If no README variant is found.
    """
    source_url = get_bookmark_source_url(bookmark)
    if not is_github_url(source_url):
        raise BookmarkContentUnavailableError(f"Bookmark {bookmark.id} is not a GitHub bookmark: {source_url}")

    if is_github_pages_url(source_url):
        html = html_content or get_bookmark_html_content(bookmark)
        if html and html.strip():
            return html.encode("utf-8")
        raise BookmarkContentUnavailableError(f"Bookmark {bookmark.id} has no HTML content for GitHub Pages URL")

    if is_github_repo_root(source_url) or source_mentions_readme(source_url):
        html = html_content or get_bookmark_html_content(bookmark)
        if html and html.strip():
            return html.encode("utf-8")

    owner, repo = parse_github_owner_repo(source_url)
    readme_variants = ["README.md", "README.MD", "readme.md", "README.rst", "README.txt", "README"]
    branches = ["main", "master"]

    async with httpx.AsyncClient(
        timeout=config.fetch_timeout_seconds if config else 30,
        follow_redirects=True,
    ) as client:
        for branch in branches:
            for readme in readme_variants:
                url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{readme}"
                # url = f"https://github.com/{owner}/{repo}/blob/{branch}/{readme}"
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        logger.info("Found GitHub README HTML: %s/%s (%s, %s)", owner, repo, branch, readme)
                        return response.content
                except httpx.HTTPError:
                    continue

    raise GitHubReadmeNotFoundError(f"No README found for {owner}/{repo}")
