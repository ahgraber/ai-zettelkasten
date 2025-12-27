"""Bookmark validation and content inspection utilities."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from aizk.utilities.arxiv_utils import is_arxiv_url
from aizk.utilities.url_utils import is_github_url
from karakeep_client.karakeep import KarakeepClient
from karakeep_client.models import Bookmark, ContentTypeAsset, ContentTypeLink, ContentTypeText

__all__ = [
    "BookmarkContentKind",
    "BookmarkContentError",
    "detect_content_type",
    "detect_source_type",
    "fetch_karakeep_bookmark",
    "get_bookmark_asset_id",
    "get_bookmark_html_content",
    "get_bookmark_source_url",
    "get_bookmark_text_content",
    "is_pdf_asset",
    "resolve_bookmark_content_type",
    "resolve_bookmark_type",
    "validate_bookmark_content",
]

logger = logging.getLogger(__name__)


class BookmarkContentError(ValueError):
    """Error raised when a KaraKeep bookmark lacks usable content."""

    error_code = "karakeep_bookmark_missing_contents"

    def __init__(self, message: str):
        super().__init__(message)
        self.error_code = self.error_code


BookmarkContentKind = Literal["link", "text", "asset", "unknown"]


def get_bookmark_source_url(bookmark: Bookmark) -> str:
    """Extract the source URL from a KaraKeep bookmark."""
    content = bookmark.content
    if isinstance(content, ContentTypeLink):
        return content.url
    if isinstance(content, ContentTypeText) and content.source_url:
        return content.source_url
    if isinstance(content, ContentTypeAsset) and content.source_url:
        return content.source_url
    raise BookmarkContentError(f"Bookmark {bookmark.id} has no source URL")


def fetch_karakeep_bookmark(karakeep_id: str) -> Bookmark | None:
    """Fetch bookmark details from KaraKeep."""

    async def _get_bookmark() -> Bookmark | None:
        async with KarakeepClient() as client:
            return await client.get_bookmark(karakeep_id)

    try:
        return asyncio.run(_get_bookmark())
    except Exception as exc:
        logger.warning("Failed to fetch KaraKeep bookmark %s: %s", karakeep_id, exc)
        return None


def get_bookmark_html_content(bookmark: Bookmark) -> str | None:
    """Return HTML content when present on a link bookmark."""
    content = bookmark.content
    if isinstance(content, ContentTypeLink):
        return content.html_content
    return None


def get_bookmark_text_content(bookmark: Bookmark) -> str | None:
    """Return text content when present on a text bookmark."""
    content = bookmark.content
    if isinstance(content, ContentTypeText):
        return content.text
    return None


def get_bookmark_asset_id(bookmark: Bookmark) -> str | None:
    """Return the asset ID when the bookmark references an asset."""
    content = bookmark.content
    if isinstance(content, ContentTypeAsset):
        return content.asset_id
    return None


def is_pdf_asset(bookmark: Bookmark) -> bool:
    """Check whether the bookmark carries a PDF asset."""
    content = bookmark.content
    return isinstance(content, ContentTypeAsset) and content.asset_type == "pdf"


def resolve_bookmark_type(bookmark: Bookmark) -> str:
    """Return the bookmark's type.

    Prefers the top-level bookmark `type` field (when present), falling back to
    the embedded content `type`. Returns "unknown" when neither exists.
    """
    bookmark_type = getattr(bookmark, "type", None)
    if bookmark_type:
        return str(bookmark_type)

    content = getattr(bookmark, "content", None)
    content_type = getattr(content, "type", None)
    return str(content_type) if content_type else "unknown"


def resolve_bookmark_content_type(bookmark: Bookmark) -> BookmarkContentKind:
    """Return a normalized content type for a bookmark."""
    content = getattr(bookmark, "content", None)
    if isinstance(content, ContentTypeLink):
        return "link"
    if isinstance(content, ContentTypeText):
        return "text"
    if isinstance(content, ContentTypeAsset):
        return "asset"
    return "unknown"


def detect_content_type(bookmark: Bookmark) -> str:
    """Detect content type from a KaraKeep bookmark.

    Args:
        bookmark: KaraKeep bookmark object.

    Returns:
        "pdf" or "html".
    """
    content = bookmark.content
    if isinstance(content, ContentTypeAsset) and content.asset_type == "pdf":
        return "pdf"
    return "html"


def detect_source_type(url: str) -> str:
    """Detect source type based on URL domain.

    Args:
        url: Source URL.

    Returns:
        "arxiv", "github", or "other".
    """
    if is_arxiv_url(url):
        return "arxiv"
    if is_github_url(url):
        return "github"
    return "other"


def validate_bookmark_content(bookmark: Bookmark) -> None:
    """Validate that the bookmark has HTML content, text, or a PDF asset."""
    html_content = get_bookmark_html_content(bookmark)
    text_content = get_bookmark_text_content(bookmark)
    if html_content and html_content.strip():
        return
    if text_content and text_content.strip():
        return
    if is_pdf_asset(bookmark):
        return
    raise BookmarkContentError(f"Bookmark {bookmark.id} is missing HTML, text, or PDF content")
