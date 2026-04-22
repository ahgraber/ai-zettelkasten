"""URL fetcher adapter implementing the ContentFetcher protocol.

Fetches content bytes for a UrlRef, supporting both KaraKeep asset URLs
(fetched via the KaraKeep client) and arbitrary HTTP URLs (fetched via httpx).
"""

from __future__ import annotations

import asyncio
import io
from typing import ClassVar
from urllib.parse import urlparse

import httpx

from aizk.conversion.core.errors import FetchError, FetchTooLargeError
from aizk.conversion.core.source_ref import SourceRef, UrlRef
from aizk.conversion.core.types import ContentType, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig, KarakeepFetcherConfig
from aizk.conversion.utilities.fetch_helpers import fetch_karakeep_asset


class UrlFetcher:
    """ContentFetcher that retrieves bytes for a UrlRef.

    Dispatches to the KaraKeep asset API for KaraKeep asset URLs and falls
    back to a plain HTTP GET for all other URLs.
    """

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF, ContentType.HTML})

    def __init__(self, config: ConversionConfig, karakeep_cfg: KarakeepFetcherConfig) -> None:
        self._config = config
        self._karakeep_cfg = karakeep_cfg

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
        karakeep_base_url = self._karakeep_cfg.base_url.rstrip("/")

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
            # Use content_type hint from resolver when available; fall back to path-suffix inference.
            if ref.content_type_hint is not None:
                content_type = ref.content_type_hint
            else:
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
            async with (
                httpx.AsyncClient(
                    timeout=self._config.fetch_timeout_seconds,
                    follow_redirects=True,
                ) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                max_bytes = self._config.fetch_max_response_bytes
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = None
                    else:
                        if declared_length > max_bytes:
                            raise FetchTooLargeError(
                                f"Response from {url!r} exceeds configured limit of {max_bytes} bytes"
                            )

                buffer = io.BytesIO()
                total_bytes = 0
                async for chunk in response.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise FetchTooLargeError(
                            f"Response from {url!r} exceeds configured limit of {max_bytes} bytes"
                        )
                    buffer.write(chunk)

                ct_header = response.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            raise FetchError(f"HTTP error fetching {url!r}: {exc}") from exc

        content_type = ContentType.PDF if "application/pdf" in ct_header else ContentType.HTML
        return buffer.getvalue(), content_type


__all__ = ["UrlFetcher"]
