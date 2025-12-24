"""URL utilities specific to the conversion service."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from aizk.utilities.url_utils import is_arxiv_url, is_github_url, strip_utm_params, validate_url


def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication.

    Args:
        url: Input URL.

    Returns:
        A normalized URL with lowercased domain, sorted query params, and no fragment.
    """
    validated = validate_url(url)
    parsed = urlparse(strip_utm_params(validated))
    query_pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    normalized_query = urlencode(query_pairs, doseq=True)
    normalized = parsed._replace(
        netloc=parsed.netloc.lower(),
        query=normalized_query,
        fragment="",
    )
    return urlunparse(normalized)


def detect_content_type(url: str, karakeep_metadata: Mapping[str, Any] | None = None) -> str:
    """Detect content type using metadata or URL patterns.

    Args:
        url: Source URL.
        karakeep_metadata: Optional metadata dict from KaraKeep.

    Returns:
        "pdf" or "html".
    """
    content_type = ""
    if karakeep_metadata:
        content_type = str(
            karakeep_metadata.get("content_type")
            or karakeep_metadata.get("mime_type")
            or karakeep_metadata.get("file_type")
            or ""
        ).lower()
    if "pdf" in content_type:
        return "pdf"
    if url.lower().split("?")[0].endswith(".pdf"):
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
