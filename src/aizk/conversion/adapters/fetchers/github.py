"""GitHub README fetcher adapter implementing the ContentFetcher protocol.

Fetches README content as HTML bytes for a GithubReadmeRef by trying
common branch names and readme filename variants.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import httpx

from aizk.conversion.core.errors import GitHubReadmeNotFoundError
from aizk.conversion.core.source_ref import GithubReadmeRef, SourceRef
from aizk.conversion.core.types import ContentType, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig


class GithubReadmeFetcher:
    """ContentFetcher that retrieves README bytes for a GithubReadmeRef."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def __init__(self, config: ConversionConfig) -> None:
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
        """
        assert isinstance(ref, GithubReadmeRef), f"Expected GithubReadmeRef, got {type(ref)}"
        content = asyncio.run(self._fetch_readme(ref))
        return ConversionInput(content=content, content_type=ContentType.HTML)

    async def _fetch_readme(self, ref: GithubReadmeRef) -> bytes:
        """Async implementation: iterate branches and readme variants."""
        branches = ["main", "master"]
        readme_variants = ["README.md", "README.MD", "readme.md", "README.rst", "README.txt", "README"]

        async with httpx.AsyncClient(
            timeout=self._config.fetch_timeout_seconds,
            follow_redirects=True,
        ) as client:
            for branch in branches:
                for readme in readme_variants:
                    url = f"https://raw.githubusercontent.com/{ref.owner}/{ref.repo}/{branch}/{readme}"
                    try:
                        response = await client.get(url)
                        if response.status_code == 200:
                            return response.content
                    except httpx.HTTPError:
                        continue

        raise GitHubReadmeNotFoundError(f"No README found for {ref.owner}/{ref.repo}")


__all__ = ["GithubReadmeFetcher"]
