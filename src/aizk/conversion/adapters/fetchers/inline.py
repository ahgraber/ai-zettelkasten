"""InlineContentFetcher adapter implementing the ContentFetcher protocol."""

from __future__ import annotations

from typing import ClassVar

from aizk.conversion.core.types import ContentType, ConversionInput


class InlineContentFetcher:
    """ContentFetcher that returns the bytes already embedded in an InlineHtmlRef."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})
    api_submittable: ClassVar[bool] = False

    def fetch(self, ref) -> ConversionInput:
        return ConversionInput(content=ref.body, content_type=ContentType.HTML)
