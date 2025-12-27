"""Content fetching utilities for conversion workers."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from aizk.conversion.utilities.bookmark_utils import (
    BookmarkContentError,
    get_bookmark_asset_id,
    get_bookmark_html_content,
    get_bookmark_source_url,
    is_pdf_asset,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.utilities.arxiv_utils import ArxivClient, get_arxiv_id, is_arxiv_url
from aizk.utilities.url_utils import standardize_github
from karakeep_client.karakeep import KarakeepClient
from karakeep_client.models import Bookmark

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """Base exception for fetch errors."""


class BookmarkContentUnavailableError(FetchError, BookmarkContentError):
    """Raised when KaraKeep bookmark has no usable content."""


class ArxivPdfFetchError(FetchError):
    """Raised when arXiv PDF fetch fails."""


class GitHubReadmeNotFoundError(FetchError):
    """Raised when GitHub README not found."""


def _is_arxiv_abstract_url(url: str) -> bool:
    """Check if URL is an arXiv abstract page."""
    try:
        parsed = urlparse(url)
        return parsed.path.startswith("/abs")
    except Exception:
        return False


async def fetch_karakeep_asset(client: KarakeepClient, asset_id: str) -> bytes:
    """Fetch asset bytes from KaraKeep.

    Args:
        client: KaraKeep client instance.
        asset_id: Asset identifier.

    Returns:
        Asset bytes.

    Raises:
        FetchError: If asset fetch fails.
    """
    try:
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
    karakeep_client: KarakeepClient | None = None,
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
    arxiv_id = get_arxiv_id(source_url) if source_url and is_arxiv_url(source_url) else None

    if not arxiv_id:
        raise BookmarkContentUnavailableError(f"Bookmark {bookmark.id} does not contain an arXiv URL")

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
        if asset_id and karakeep_client:
            logger.info("Fetching PDF asset %s from KaraKeep for arXiv %s", asset_id, arxiv_id)
            return await fetch_karakeep_asset(karakeep_client, asset_id)

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


async def fetch_github_readme(github_url: str, config: ConversionConfig) -> bytes:
    """Fetch GitHub README.

    Attempts to fetch README from repository, trying common variants across
    main/master branches.

    Args:
        github_url: GitHub repository URL (e.g., https://github.com/owner/repo).
        config: Conversion configuration.

    Returns:
        README content as bytes.

    Raises:
        GitHubReadmeNotFoundError: If no README variant is found.
    """
    std_url = standardize_github(github_url)
    if not std_url.startswith("https://github.com/"):
        raise ValueError(f"Invalid GitHub URL: {github_url}")

    parts = std_url.rstrip("/").split("/")
    if len(parts) < 5:
        raise ValueError(f"Cannot parse owner/repo from URL: {github_url}")
    owner, repo = parts[3], parts[4]

    readme_variants = ["README.md", "README.MD", "readme.md", "README.rst", "README.txt", "README"]
    branches = ["main", "master"]

    async with httpx.AsyncClient(timeout=config.fetch_timeout_seconds, follow_redirects=True) as client:
        for branch in branches:
            for readme in readme_variants:
                url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{readme}"
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        logger.info("Found GitHub README: %s/%s (%s, %s)", owner, repo, branch, readme)
                        return response.content
                except httpx.HTTPError:
                    continue

    raise GitHubReadmeNotFoundError(f"No README found for {owner}/{repo}")
