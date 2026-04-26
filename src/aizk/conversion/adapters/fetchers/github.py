"""GitHub README fetcher adapter implementing the ContentFetcher protocol.

Fetches README content as HTML bytes for a GithubReadmeRef by trying
common branch names and readme filename variants. All HTTP traffic flows
through the egress-validated helper (deny-list policy, connection pinning,
manual redirect loop).
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from aizk.conversion.core.errors import EgressPolicyError, FetchError, GitHubReadmeNotFoundError
from aizk.conversion.core.source_ref import GithubReadmeRef, SourceRef
from aizk.conversion.core.types import ContentType, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.egress_fetch import egress_fetch_bytes


class GithubReadmeFetcher:
    """ContentFetcher that retrieves README bytes for a GithubReadmeRef."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def __init__(self, config: ConversionConfig) -> None:
        """Initialize the fetcher with the conversion config (used for byte caps)."""
        self._config = config

    def fetch(self, ref: SourceRef) -> ConversionInput:
        """Fetch README HTML bytes for ``ref``.

        Tries ``main`` then ``master`` branches, and for each branch tries
        ``README.md``, ``README.MD``, ``readme.md``, ``README.rst``,
        ``README.txt``, and ``README`` in order.

        Args:
            ref: A GithubReadmeRef to fetch.

        Returns:
            ConversionInput with README content bytes and ContentType.HTML.

        Raises:
            GitHubReadmeNotFoundError: If no README variant is found.
            EgressPolicyError: If the egress policy rejects the destination
                (non-retryable; propagated unchanged).
        """
        assert isinstance(ref, GithubReadmeRef), f"Expected GithubReadmeRef, got {type(ref)}"
        content = asyncio.run(self._fetch_readme(ref))
        return ConversionInput(content=content, content_type=ContentType.HTML)

    async def _fetch_readme(self, ref: GithubReadmeRef) -> bytes:
        """Iterate branches and readme variants, returning the first 200-OK body.

        Each candidate URL is fetched through ``egress_fetch_bytes``. Egress
        policy violations propagate as ``EgressPolicyError`` (non-retryable),
        not silently skipped — only generic ``FetchError`` (e.g. 404 on a
        readme variant that doesn't exist) is treated as "try the next one".
        """
        branches = ["main", "master"]
        readme_variants = ["README.md", "README.MD", "readme.md", "README.rst", "README.txt", "README"]

        for branch in branches:
            for readme in readme_variants:
                url = f"https://raw.githubusercontent.com/{ref.owner}/{ref.repo}/{branch}/{readme}"
                try:
                    body, _headers = await egress_fetch_bytes(
                        url,
                        max_response_bytes=self._config.fetch_max_response_bytes,
                    )
                except EgressPolicyError:
                    raise
                except FetchError:
                    continue
                return body

        raise GitHubReadmeNotFoundError(f"No README found for {ref.owner}/{ref.repo}")


__all__ = ["GithubReadmeFetcher"]
