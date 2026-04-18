"""SingleFileFetcher skeleton — not yet registered or functional."""

from __future__ import annotations

from typing import ClassVar

from aizk.conversion.core.types import ContentType, ConversionInput


class SingleFileFetcher:
    """Skeleton ContentFetcher for SingleFileRef ingress.

    Not registered in the shared registration helper and therefore not
    accepted at the API or worker boundary.  This class exists to hold
    the intended interface until the implementation lands.
    """

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def fetch(self, ref) -> ConversionInput:
        raise NotImplementedError(
            "SingleFileFetcher is not implemented yet and is not registered "
            "with the conversion pipeline."
        )
