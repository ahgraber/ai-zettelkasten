"""UrlFetcher adapter implementing the ContentFetcher protocol."""

from __future__ import annotations

import logging
from typing import ClassVar

from aizk.conversion.core.types import ContentType, ConversionInput

logger = logging.getLogger(__name__)

_PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/x-pdf"})


def _detect_content_type(url: str, content_type_header: str) -> ContentType:
    """Infer ContentType from the response Content-Type header or URL suffix."""
    ct = content_type_header.split(";")[0].strip().lower()
    if ct in _PDF_CONTENT_TYPES:
        return ContentType.PDF
    if url.lower().rstrip("/").endswith(".pdf"):
        return ContentType.PDF
    return ContentType.HTML


class UrlFetcher:
    """ContentFetcher that retrieves content from an arbitrary URL via HTTP GET."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF, ContentType.HTML})
    api_submittable: ClassVar[bool] = False

    def __init__(self, config=None) -> None:
        self._config = config

    def fetch(self, ref) -> ConversionInput:
        from aizk.conversion.utilities.safe_http import BodyTooLargeError, UnsafeUrlError, safe_get
        from aizk.conversion.workers.fetcher import FetchError

        cfg = self._config
        timeout = float(cfg.fetch_timeout_seconds) if cfg else 30.0

        url = ref.url
        logger.info("Fetching URL: %s", url)
        try:
            result = safe_get(url, timeout=timeout)
        except (UnsafeUrlError, BodyTooLargeError):
            # Hardened-boundary rejections are non-retryable; re-raise as-is
            # so the classifier sees the typed error_code/retryable fields.
            raise
        except Exception as exc:
            raise FetchError(f"Failed to fetch {url}: {exc}") from exc

        content_type = _detect_content_type(url, result.headers.get("content-type", ""))
        return ConversionInput(
            content=result.content,
            content_type=content_type,
            metadata={"source_url": result.final_url},
        )
