"""GitHub README fetcher adapter implementing the ContentFetcher protocol.

Fetches README content as HTML bytes for a GithubReadmeRef by trying
common branch names and readme filename variants. All HTTP traffic flows
through the egress-validated helper (deny-list policy, connection pinning,
manual redirect loop).
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from aizk.conversion.core.errors import EgressPolicyError, FetchError, GitHubReadmeNotFoundError
from aizk.conversion.core.protocols import ContentFetcher
from aizk.conversion.core.source_ref import GithubReadmeRef, SourceRef
from aizk.conversion.core.types import ContentType, ConversionInput, SourceMetadata
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.egress_fetch import egress_fetch_bytes

_logger = logging.getLogger(__name__)


class GithubReadmeFetcher(ContentFetcher):
    """ContentFetcher that retrieves README bytes for a GithubReadmeRef."""

    produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def __init__(self, config: ConversionConfig) -> None:
        """Initialize the fetcher with the conversion config (used for byte caps)."""
        self._config = config

    def fetch(self, ref: SourceRef, source_meta: SourceMetadata) -> ConversionInput:
        """Fetch README HTML bytes for ``ref`` and populate source URL fields.

        Populates ``source_url``, ``document_base_url``, and ``normalized_url``
        from the constructed README URL when the resolver has not already set them
        (merge semantics: resolver-supplied values win).

        Tries ``main`` then ``master`` branches, and for each branch tries
        ``README.md``, ``README.MD``, ``readme.md``, ``README.rst``,
        ``README.txt``, and ``README`` in order.

        Args:
            ref: A GithubReadmeRef to fetch.
            source_meta: Accumulated source metadata from any resolver hops.

        Returns:
            ConversionInput with README content bytes, ContentType.HTML, and merged source_meta.

        Raises:
            GitHubReadmeNotFoundError: If no README variant is found.
            EgressPolicyError: If the egress policy rejects the destination
                (non-retryable; propagated unchanged).
        """
        from aizk.utilities.url_utils import normalize_url

        if not isinstance(ref, GithubReadmeRef):
            raise TypeError(f"Expected GithubReadmeRef, got {type(ref).__name__}")

        content, readme_url = asyncio.run(self._fetch_readme(ref))

        # Only contribute the source-URL trio when upstream did not supply source_url.
        # Backfilling individual fields would risk producing normalized_url derived from
        # the raw README URL while source_url remains the canonical github.com URL —
        # violating the conversion-worker spec invariant that
        # `normalized_url == normalize_url(source_url)`.
        if source_meta.source_url is None:
            observed_normalized: str | None = None
            try:
                observed_normalized = normalize_url(readme_url)
            except Exception:
                _logger.debug(
                    "normalize_url failed for GithubReadmeFetcher source_url=%r; normalized_url=None",
                    readme_url,
                )
            observed = SourceMetadata(
                source_url=readme_url,
                normalized_url=observed_normalized,
                document_base_url=readme_url,
            )
            merged = source_meta.merge(observed)
        else:
            merged = source_meta

        return ConversionInput(content=content, content_type=ContentType.HTML, source_meta=merged)

    async def _fetch_readme(self, ref: GithubReadmeRef) -> tuple[bytes, str]:
        """Iterate branches and readme variants, returning the first 200-OK body and its URL.

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
                return body, url

        raise GitHubReadmeNotFoundError(f"No README found for {ref.owner}/{ref.repo}")


__all__ = ["GithubReadmeFetcher"]
