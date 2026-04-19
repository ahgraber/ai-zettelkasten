"""Inline content fetcher adapter implementing the ContentFetcher protocol.

Passes through InlineHtmlRef body bytes directly — no network I/O.
"""

from __future__ import annotations

from typing import ClassVar

from aizk.conversion.core.source_ref import InlineHtmlRef, SourceRef
from aizk.conversion.core.types import ContentType, ConversionInput


class InlineContentFetcher:
    """ContentFetcher that returns embedded InlineHtmlRef bytes without any I/O."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def fetch(self, ref: SourceRef) -> ConversionInput:
        """Return the body bytes from an InlineHtmlRef as a ConversionInput.

        Args:
            ref: An InlineHtmlRef carrying the HTML body.

        Returns:
            ConversionInput with body bytes and ContentType.HTML.
        """
        assert isinstance(ref, InlineHtmlRef), f"Expected InlineHtmlRef, got {type(ref)}"
        return ConversionInput(content=ref.body, content_type=ContentType.HTML)


__all__ = ["InlineContentFetcher"]
