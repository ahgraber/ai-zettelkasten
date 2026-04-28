"""URL fetcher adapter implementing the ContentFetcher protocol.

Fetches content bytes for a UrlRef, supporting both KaraKeep asset URLs
(fetched via the KaraKeep client) and arbitrary HTTP URLs (fetched via the
egress-validated httpx helper that enforces deny-list policy, connection
pinning, and per-hop redirect validation).
"""

from __future__ import annotations

import asyncio
from typing import ClassVar
from urllib.parse import urlparse

from aizk.conversion.core.errors import EgressPolicyError, FetchError
from aizk.conversion.core.source_ref import SourceRef, UrlRef
from aizk.conversion.core.types import ContentType, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig, KarakeepFetcherConfig
from aizk.conversion.utilities.egress_fetch import egress_fetch_bytes
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
        if not isinstance(ref, UrlRef):
            raise TypeError(f"Expected UrlRef, got {type(ref).__name__}")

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

        # Generic HTTP URL — EgressPolicyError must propagate unwrapped so
        # job retry classification sees the non-retryable signal.
        try:
            content, content_type = asyncio.run(self._fetch_http(url))
        except (EgressPolicyError, FetchError):
            raise
        except Exception as exc:
            raise FetchError(f"Failed to fetch URL {url!r}: {exc}") from exc
        return ConversionInput(content=content, content_type=content_type)

    async def _fetch_http(self, url: str) -> tuple[bytes, ContentType]:
        """Fetch ``url`` via the egress-validated helper and detect content type.

        Delegates to ``egress_fetch_bytes`` which enforces deny-list egress
        policy, connection pinning to a pre-resolved IP (defeats DNS-rebinding
        TOCTOU), a manual redirect loop with per-hop validation, and
        cross-host header hygiene. Content type is inferred from the
        terminal response's ``content-type`` header.
        """
        body, response_headers = await egress_fetch_bytes(
            url,
            max_response_bytes=self._config.fetch_max_response_bytes,
        )
        ct_header = response_headers.get("content-type", "")
        content_type = ContentType.PDF if "application/pdf" in ct_header else ContentType.HTML
        return body, content_type


__all__ = ["UrlFetcher"]
