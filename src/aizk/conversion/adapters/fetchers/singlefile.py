"""SingleFile fetcher adapter skeleton (not yet implemented)."""

from __future__ import annotations

from typing import ClassVar

from aizk.conversion.core.source_ref import SourceRef
from aizk.conversion.core.types import ContentType, ConversionInput


class SingleFileFetcher:
    """Skeleton ContentFetcher for SingleFile-archived pages. Not yet implemented."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def fetch(self, ref: SourceRef) -> ConversionInput:
        raise NotImplementedError("SingleFileFetcher is not yet implemented")


__all__ = ["SingleFileFetcher"]
