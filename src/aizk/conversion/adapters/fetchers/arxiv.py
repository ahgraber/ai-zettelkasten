"""ArxivFetcher adapter implementing the ContentFetcher protocol."""

from __future__ import annotations

import logging
from typing import ClassVar

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

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})
    api_submittable: ClassVar[bool] = False

    def __init__(self, config=None) -> None:
        self._config = config

    def fetch(self, ref) -> ConversionInput:
        from aizk.conversion.utilities.safe_http import BodyTooLargeError, UnsafeUrlError, safe_get
        from aizk.conversion.workers.fetcher import ArxivPdfFetchError

        cfg = self._config
        timeout = float(cfg.fetch_timeout_seconds) if cfg else 30.0

        abstract_url = f"https://arxiv.org/abs/{ref.arxiv_id}"

        def _hardened_get(target: str, *, step_label: str) -> ConversionInput:
            try:
                result = safe_get(target, timeout=timeout)
            except (UnsafeUrlError, BodyTooLargeError):
                raise
            except Exception as exc:
                raise ArxivPdfFetchError(
                    f"Failed to fetch {step_label} for arXiv {ref.arxiv_id}: {exc}"
                ) from exc
            return ConversionInput(
                content=result.content,
                content_type=ContentType.PDF,
                metadata={"source_url": abstract_url, "arxiv_id": ref.arxiv_id},
            )

        # Step 1: KaraKeep asset URL (preferred — avoids arxiv.org rate limits)
        if getattr(ref, "karakeep_asset_url", None):
            logger.info("Fetching arXiv PDF from KaraKeep asset: %s", ref.karakeep_asset_url)
            return _hardened_get(ref.karakeep_asset_url, step_label="KaraKeep asset")

        # Step 2: explicit arxiv_pdf_url on the ref
        if getattr(ref, "arxiv_pdf_url", None):
            logger.info("Fetching arXiv PDF from explicit URL: %s", ref.arxiv_pdf_url)
            return _hardened_get(ref.arxiv_pdf_url, step_label="arXiv PDF URL")

        # Step 3: construct PDF URL from arxiv_id via arxiv.org/pdf/<id>.pdf.
        # The legacy ArxivClient path is replaced with a direct safe_get so
        # the SSRF + body-cap checks apply to cross-origin redirects arxiv
        # may issue (e.g. export.arxiv.org → arxiv.org mirrors).
        pdf_url = f"https://arxiv.org/pdf/{ref.arxiv_id}"
        logger.info("Fetching arXiv PDF by ID: %s", ref.arxiv_id)
        return _hardened_get(pdf_url, step_label=f"arXiv PDF by id {ref.arxiv_id}")
