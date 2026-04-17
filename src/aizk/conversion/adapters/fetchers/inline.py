"""InlineContentFetcher adapter implementing the ContentFetcher protocol."""

from __future__ import annotations

from aizk.conversion.core.types import ContentType, ConversionInput


class InlineContentFetcher:
    """ContentFetcher that returns the bytes already embedded in an InlineHtmlRef."""

    def fetch(self, ref) -> ConversionInput:
        return ConversionInput(content=ref.body, content_type=ContentType.HTML)
