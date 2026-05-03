"""ArXiv fetcher adapter implementing the ContentFetcher protocol.

Fetches PDF bytes for an ArxivRef using a 3-step source-precedence chain:
1. KaraKeep asset URL (pre-fetched PDF stored in KaraKeep)
2. Arbitrary arxiv_pdf_url (direct HTTP download via the egress-validated helper)
3. Abstract-page resolution via the ArXiv export API
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar
from urllib.parse import urlparse

from aizk.conversion.core.protocols import ContentFetcher
from aizk.conversion.core.source_ref import ArxivRef, SourceRef
from aizk.conversion.core.types import ContentType, ConversionInput, SourceMetadata
from aizk.conversion.utilities.config import ConversionConfig, KarakeepFetcherConfig
from aizk.conversion.utilities.egress_fetch import egress_fetch_bytes
from aizk.conversion.utilities.fetch_helpers import (
    fetch_arxiv_pdf,
    fetch_karakeep_asset,
    is_karakeep_trusted_asset_url,
)

_logger = logging.getLogger(__name__)


class ArxivFetcher(ContentFetcher):
    """ContentFetcher that retrieves PDF bytes for an ArxivRef."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})

    def __init__(self, config: ConversionConfig, karakeep_cfg: KarakeepFetcherConfig) -> None:
        """Initialize with conversion and KaraKeep configs."""
        self._config = config
        self._karakeep_cfg = karakeep_cfg

    def fetch(self, ref: SourceRef, source_meta: SourceMetadata) -> ConversionInput:
        """Fetch PDF bytes for ``ref`` and populate source URL fields when the resolver didn't.

        Populates ``source_url``, ``document_base_url``, and ``normalized_url`` from the
        ArXiv abstract page URL when the resolver has not already set them (merge semantics:
        resolver-supplied values win).

        Precedence for bytes:
        1. KaraKeep asset URL stored in ``ref.arxiv_pdf_url``
        2. Direct HTTP download of ``ref.arxiv_pdf_url`` (non-KaraKeep URL)
        3. ArXiv export API resolution from ``ref.arxiv_id``

        Args:
            ref: An ArxivRef to fetch.
            source_meta: Accumulated source metadata from any resolver hops.

        Returns:
            ConversionInput with PDF bytes, ContentType.PDF, and merged source_meta.

        Raises:
            ArxivPdfFetchError: If PDF cannot be fetched from arXiv.
            FetchError: If the KaraKeep asset fetch fails.
        """
        from aizk.utilities.url_utils import normalize_url

        if not isinstance(ref, ArxivRef):
            raise TypeError(f"Expected ArxivRef, got {type(ref).__name__}")

        karakeep_base_url = self._karakeep_cfg.base_url

        # Construct the ArXiv abstract page URL as our authoritative source URL.
        arxiv_source_url = f"https://arxiv.org/abs/{ref.arxiv_id}"
        observed_normalized: str | None = None
        try:
            observed_normalized = normalize_url(arxiv_source_url)
        except Exception:
            _logger.debug(
                "normalize_url failed for ArxivFetcher source_url=%r; normalized_url=None",
                arxiv_source_url,
            )

        observed = SourceMetadata(
            source_url=arxiv_source_url,
            normalized_url=observed_normalized,
            document_base_url=arxiv_source_url,
        )
        merged = source_meta.merge(observed)

        # Step 1 — KaraKeep asset URL (parsed-origin exact match, not string prefix)
        if ref.arxiv_pdf_url and is_karakeep_trusted_asset_url(ref.arxiv_pdf_url, karakeep_base_url):
            parsed = urlparse(ref.arxiv_pdf_url)
            asset_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            pdf_bytes = asyncio.run(fetch_karakeep_asset(asset_id))
            return ConversionInput(content=pdf_bytes, content_type=ContentType.PDF, source_meta=merged)

        # Step 2 — direct HTTP download of arxiv_pdf_url (non-KaraKeep)
        if ref.arxiv_pdf_url:
            response = asyncio.run(_fetch_url(ref.arxiv_pdf_url, self._config))
            return ConversionInput(content=response, content_type=ContentType.PDF, source_meta=merged)

        # Step 3 — abstract-page resolution via ArXiv API
        pdf_bytes = asyncio.run(fetch_arxiv_pdf(ref.arxiv_id, self._config))
        return ConversionInput(content=pdf_bytes, content_type=ContentType.PDF, source_meta=merged)


async def _fetch_url(url: str, config: ConversionConfig) -> bytes:
    """Fetch ``url`` bytes through the egress-validated helper.

    Delegates to ``egress_fetch_bytes`` so the per-hop deny-list policy,
    connection-pinned transport, and redirect-loop hygiene apply uniformly
    to direct ``arxiv_pdf_url`` downloads.
    """
    body, _headers = await egress_fetch_bytes(
        url,
        max_response_bytes=config.fetch_max_response_bytes,
    )
    return body


__all__ = ["ArxivFetcher"]
