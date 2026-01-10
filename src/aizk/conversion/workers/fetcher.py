"""Content fetching utilities for conversion workers."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from aizk.conversion.utilities.arxiv_utils import ArxivClient, get_arxiv_id, is_arxiv_url
from aizk.conversion.utilities.bookmark_utils import (
    BookmarkContentError,
    get_bookmark_asset_id,
    get_bookmark_html_content,
    get_bookmark_source_url,
    is_pdf_asset,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.github_utils import (
    is_github_pages_url,
    is_github_repo_root,
    is_github_url,
    parse_github_owner_repo,
    source_mentions_readme,
)
from karakeep_client.karakeep import KarakeepClient
from karakeep_client.models import Bookmark

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """Base exception for fetch errors.

    Network and remote fetch errors are typically transient and
    should be retried.
    """

    error_code = "fetch_error"
    retryable = True


class BookmarkContentUnavailableError(FetchError, BookmarkContentError):
    """Raised when KaraKeep bookmark has no usable content.

    Permanent: mirrors BookmarkContentError semantics. Explicitly override
    retryable to False to avoid MRO picking FetchError.retryable=True.
    """

    retryable = False


class ArxivPdfFetchError(FetchError):
    """Raised when arXiv PDF fetch fails."""

    error_code = "arxiv_pdf_fetch_failed"


class GitHubReadmeNotFoundError(FetchError):
    """Raised when GitHub README not found.

    Permanent: repository has no README content reachable via typical
    locations; retrying will not change the outcome.
    """

    error_code = "github_readme_not_found"
    retryable = False


def _is_arxiv_abstract_url(url: str) -> bool:
    """Check if URL is an arXiv abstract page."""
    try:
        parsed = urlparse(url)
        return parsed.path.startswith("/abs")
    except Exception:
        return False


async def fetch_karakeep_asset(asset_id: str) -> bytes:
    """Fetch asset bytes from KaraKeep.

    Args:
        asset_id: Asset identifier.

    Returns:
        Asset bytes.

    Raises:
        FetchError: If asset fetch fails.
    """
    try:
        async with KarakeepClient() as client:
            return await client.get_asset(asset_id=asset_id)
    except Exception as exc:
        raise FetchError(f"Failed to fetch KaraKeep asset {asset_id}: {exc}") from exc


async def fetch_arxiv_pdf(arxiv_id: str, config: ConversionConfig) -> bytes:
    """Fetch PDF from arXiv.

    Args:
        arxiv_id: arXiv paper ID.
        config: Conversion configuration.

    Returns:
        PDF content as bytes.

    Raises:
        ArxivPdfFetchError: If PDF fetch fails.
    """
    logger.info("Fetching arXiv PDF by id: %s", arxiv_id)

    try:
        async with ArxivClient(timeout=float(config.fetch_timeout_seconds)) as client:
            return await client.download_paper_pdf(arxiv_id, use_export_url=True)
    except Exception as exc:
        raise ArxivPdfFetchError(f"Failed to fetch arXiv PDF for {arxiv_id}: {exc}") from exc


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
