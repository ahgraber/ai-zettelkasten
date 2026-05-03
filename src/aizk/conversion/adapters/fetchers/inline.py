"""Inline content fetcher adapter implementing the ContentFetcher protocol.

Passes through InlineHtmlRef body bytes directly — no network I/O.
"""

from __future__ import annotations

from typing import ClassVar

from aizk.conversion.core.protocols import ContentFetcher
from aizk.conversion.core.source_ref import InlineHtmlRef, SourceRef
from aizk.conversion.core.types import ContentType, ConversionInput, SourceMetadata


class InlineContentFetcher(ContentFetcher):
    """ContentFetcher that returns embedded InlineHtmlRef bytes without any I/O."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def fetch(self, ref: SourceRef, source_meta: SourceMetadata) -> ConversionInput:
        """Return the body bytes from an InlineHtmlRef as a ConversionInput.

        Passes ``source_meta`` through unchanged — there is no URL to observe
        for inline content.

        Args:
            ref: An InlineHtmlRef carrying the HTML body.
            source_meta: Accumulated source metadata; passed through unmodified.

        Returns:
            ConversionInput with body bytes, ContentType.HTML, and the supplied source_meta.
        """
        if not isinstance(ref, InlineHtmlRef):
            raise TypeError(f"Expected InlineHtmlRef, got {type(ref).__name__}")
        return ConversionInput(content=ref.body, content_type=ContentType.HTML, source_meta=source_meta)


__all__ = ["InlineContentFetcher"]
