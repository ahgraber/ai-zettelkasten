"""Shared async HTTP fetch helpers used by fetcher adapters and workers."""

from __future__ import annotations

import logging

from aizk.conversion.core.errors import ArxivPdfFetchError, FetchError
from aizk.conversion.utilities.arxiv_utils import ArxivClient
from aizk.conversion.utilities.config import ConversionConfig
from karakeep_client.karakeep import KarakeepClient

logger = logging.getLogger(__name__)


async def fetch_karakeep_asset(asset_id: str) -> bytes:
    """Fetch asset bytes from KaraKeep by asset ID.

    Raises:
        FetchError: If the asset fetch fails.
    """
    try:
        async with KarakeepClient() as client:
            return await client.get_asset(asset_id=asset_id)
    except Exception as exc:
        raise FetchError(f"Failed to fetch KaraKeep asset {asset_id}: {exc}") from exc


async def fetch_arxiv_pdf(arxiv_id: str, config: ConversionConfig) -> bytes:
    """Fetch PDF from arXiv by paper ID.

    Raises:
        ArxivPdfFetchError: If the PDF fetch fails.
    """
    logger.info("Fetching arXiv PDF by id: %s", arxiv_id)
    try:
        async with ArxivClient(timeout=float(config.fetch_timeout_seconds)) as client:
            return await client.download_paper_pdf(arxiv_id, use_export_url=True)
    except Exception as exc:
        raise ArxivPdfFetchError(f"Failed to fetch arXiv PDF for {arxiv_id}: {exc}") from exc


__all__ = ["fetch_arxiv_pdf", "fetch_karakeep_asset"]
