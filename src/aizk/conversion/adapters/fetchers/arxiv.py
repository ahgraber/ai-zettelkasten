"""ArxivFetcher adapter implementing the ContentFetcher protocol."""

from __future__ import annotations

import logging

from aizk.conversion.core.types import ContentType, ConversionInput

logger = logging.getLogger(__name__)


class ArxivFetcher:
    """ContentFetcher that retrieves the PDF for an ArxivRef.

    Sub-precedence for PDF source (preserving legacy orchestrator behaviour):

    1. ``karakeep_asset_url`` set on the ref → fetch from KaraKeep asset endpoint
       (preferred: avoids arxiv.org rate limits)
    2. ``arxiv_pdf_url`` set on the ref → fetch from that explicit PDF URL
    3. Otherwise → construct PDF URL from ``arxiv_id`` and fetch from arxiv.org
    """

    def __init__(self, config=None) -> None:
        self._config = config

    def fetch(self, ref) -> ConversionInput:
        import asyncio

        import httpx

        from aizk.conversion.utilities.arxiv_utils import ArxivClient, arxiv_pdf_url
        from aizk.conversion.utilities.config import ConversionConfig
        from aizk.conversion.workers.fetcher import ArxivPdfFetchError

        cfg = self._config
        timeout = float(cfg.fetch_timeout_seconds) if cfg else 30.0

        # Step 1: KaraKeep asset URL (preferred — avoids arxiv.org rate limits)
        if getattr(ref, "karakeep_asset_url", None):
            logger.info("Fetching arXiv PDF from KaraKeep asset: %s", ref.karakeep_asset_url)
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    response = client.get(ref.karakeep_asset_url)
                    response.raise_for_status()
                    return ConversionInput(content=response.content, content_type=ContentType.PDF)
            except Exception as exc:
                raise ArxivPdfFetchError(
                    f"Failed to fetch KaraKeep asset for arXiv {ref.arxiv_id}: {exc}"
                ) from exc

        # Step 2: explicit arxiv_pdf_url on the ref
        if getattr(ref, "arxiv_pdf_url", None):
            logger.info("Fetching arXiv PDF from explicit URL: %s", ref.arxiv_pdf_url)
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    response = client.get(ref.arxiv_pdf_url)
                    response.raise_for_status()
                    return ConversionInput(content=response.content, content_type=ContentType.PDF)
            except Exception as exc:
                raise ArxivPdfFetchError(
                    f"Failed to fetch arXiv PDF from URL for {ref.arxiv_id}: {exc}"
                ) from exc

        # Step 3: construct PDF URL from arxiv_id
        logger.info("Fetching arXiv PDF by ID: %s", ref.arxiv_id)
        try:
            async def _download() -> bytes:
                async with ArxivClient(timeout=timeout) as client:
                    return await client.download_paper_pdf(ref.arxiv_id, use_export_url=True)

            pdf_bytes = asyncio.run(_download())
            return ConversionInput(content=pdf_bytes, content_type=ContentType.PDF)
        except Exception as exc:
            raise ArxivPdfFetchError(
                f"Failed to fetch arXiv PDF for {ref.arxiv_id}: {exc}"
            ) from exc
