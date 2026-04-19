"""URL fetcher adapter implementing the ContentFetcher protocol.

Fetches content bytes for a UrlRef, supporting both KaraKeep asset URLs
(fetched via the KaraKeep client) and arbitrary HTTP URLs (fetched via httpx).
"""

from __future__ import annotations

import asyncio
import os
from typing import ClassVar
from urllib.parse import urlparse

import httpx

from aizk.conversion.core.source_ref import SourceRef, UrlRef
from aizk.conversion.core.types import ContentType, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers.fetcher import FetchError, fetch_karakeep_asset


class UrlFetcher:
    """ContentFetcher that retrieves bytes for a UrlRef.

    Dispatches to the KaraKeep asset API for KaraKeep asset URLs and falls
    back to a plain HTTP GET for all other URLs.
    """

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF, ContentType.HTML})

    def __init__(self, config: ConversionConfig) -> None:
        self._config = config

    def fetch(self, ref: SourceRef) -> ConversionInput:
        """Fetch bytes for ``ref``.

        Args:
            ref: A UrlRef to fetch.

        Returns:
            ConversionInput with content bytes and detected ContentType.

        Raises:
            FetchError: On network or fetch failure.
        """
        assert isinstance(ref, UrlRef), f"Expected UrlRef, got {type(ref)}"

        url = ref.url
        karakeep_base_url = os.environ.get("KARAKEEP_BASE_URL", "").rstrip("/")

        # KaraKeep asset URL: extract asset_id from last path segment
        if karakeep_base_url and url.startswith(karakeep_base_url):
            parsed = urlparse(url)
            asset_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            try:
                content = asyncio.run(fetch_karakeep_asset(asset_id))
            except FetchError:
                raise
            except Exception as exc:
                raise FetchError(f"Failed to fetch KaraKeep asset for URL {url!r}: {exc}") from exc
            # Detect content type from URL path
            content_type = ContentType.PDF if parsed.path.lower().endswith(".pdf") else ContentType.HTML
            return ConversionInput(content=content, content_type=content_type)

        # Generic HTTP URL
        try:
            content, content_type = asyncio.run(self._fetch_http(url))
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"Failed to fetch URL {url!r}: {exc}") from exc
        return ConversionInput(content=content, content_type=content_type)

    async def _fetch_http(self, url: str) -> tuple[bytes, ContentType]:
        """Async helper: GET a URL and detect content type from response headers."""
        try:
            async with httpx.AsyncClient(
                timeout=self._config.fetch_timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP error fetching {url!r}: {exc}") from exc

        ct_header = response.headers.get("content-type", "")
        content_type = ContentType.PDF if "application/pdf" in ct_header else ContentType.HTML
        return response.content, content_type


__all__ = ["UrlFetcher"]
