"""ArXiv fetcher adapter implementing the ContentFetcher protocol.

Fetches PDF bytes for an ArxivRef using a 3-step source-precedence chain:
1. KaraKeep asset URL (pre-fetched PDF stored in KaraKeep)
2. Arbitrary arxiv_pdf_url (direct HTTP download)
3. Abstract-page resolution via the ArXiv export API
"""

from __future__ import annotations

import asyncio
from typing import ClassVar
from urllib.parse import urlparse

import httpx

from aizk.conversion.core.source_ref import ArxivRef, SourceRef
from aizk.conversion.core.types import ContentType, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig, KarakeepFetcherConfig
from aizk.conversion.workers.fetcher import fetch_arxiv_pdf, fetch_karakeep_asset


class ArxivFetcher:
    """ContentFetcher that retrieves PDF bytes for an ArxivRef."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})

    def __init__(self, config: ConversionConfig, karakeep_cfg: KarakeepFetcherConfig) -> None:
        self._config = config
        self._karakeep_cfg = karakeep_cfg

    def fetch(self, ref: SourceRef) -> ConversionInput:
        """Fetch PDF bytes for ``ref``.

        Precedence:
        1. KaraKeep asset URL stored in ``ref.arxiv_pdf_url``
        2. Direct HTTP download of ``ref.arxiv_pdf_url`` (non-KaraKeep URL)
        3. ArXiv export API resolution from ``ref.arxiv_id``

        Args:
            ref: An ArxivRef to fetch.

        Returns:
            ConversionInput with PDF bytes and ContentType.PDF.

        Raises:
            ArxivPdfFetchError: If PDF cannot be fetched from arXiv.
            FetchError: If the KaraKeep asset fetch fails.
        """
        assert isinstance(ref, ArxivRef), f"Expected ArxivRef, got {type(ref)}"

        karakeep_base_url = self._karakeep_cfg.base_url.rstrip("/")

        # Step 1 — KaraKeep asset URL
        if ref.arxiv_pdf_url and karakeep_base_url and ref.arxiv_pdf_url.startswith(karakeep_base_url):
            parsed = urlparse(ref.arxiv_pdf_url)
            asset_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            pdf_bytes = asyncio.run(fetch_karakeep_asset(asset_id))
            return ConversionInput(content=pdf_bytes, content_type=ContentType.PDF)

        # Step 2 — direct HTTP download of arxiv_pdf_url (non-KaraKeep)
        if ref.arxiv_pdf_url:
            response = asyncio.run(_fetch_url(ref.arxiv_pdf_url, self._config.fetch_timeout_seconds))
            return ConversionInput(content=response, content_type=ContentType.PDF)

        # Step 3 — abstract-page resolution via ArXiv API
        pdf_bytes = asyncio.run(fetch_arxiv_pdf(ref.arxiv_id, self._config))
        return ConversionInput(content=pdf_bytes, content_type=ContentType.PDF)


async def _fetch_url(url: str, timeout: int) -> bytes:
    """Async helper: GET a URL and return response bytes."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


__all__ = ["ArxivFetcher"]
