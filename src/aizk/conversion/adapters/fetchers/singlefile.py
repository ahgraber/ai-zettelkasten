"""SingleFileFetcher skeleton — not yet registered or functional."""

from __future__ import annotations

from aizk.conversion.core.types import ConversionInput


class SingleFileFetcher:
    """Skeleton ContentFetcher for SingleFileRef ingress.

    Not registered in the shared registration helper and therefore not
    accepted at the API or worker boundary.  This class exists to hold
    the intended interface until the implementation lands.
    """

    def fetch(self, ref) -> ConversionInput:
        raise NotImplementedError(
            "SingleFileFetcher is not implemented yet and is not registered "
            "with the conversion pipeline."
        )
