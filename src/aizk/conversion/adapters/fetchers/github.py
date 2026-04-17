"""GithubReadmeFetcher adapter implementing the ContentFetcher protocol."""

from __future__ import annotations

import logging

from aizk.conversion.core.types import ContentType, ConversionInput

logger = logging.getLogger(__name__)


class GithubReadmeFetcher:
    """ContentFetcher that retrieves a GitHub repository's README as HTML bytes.

    Iterates through common README filenames and branch names (main, master)
    using the raw.githubusercontent.com endpoint.
    """

    def __init__(self, config=None) -> None:
        self._config = config

    def fetch(self, ref) -> ConversionInput:
        import asyncio

        import httpx

        from aizk.conversion.workers.fetcher import GitHubReadmeNotFoundError

        cfg = self._config
        timeout = float(cfg.fetch_timeout_seconds) if cfg else 30.0
        owner = ref.owner
        repo = ref.repo

        readme_variants = ["README.md", "README.MD", "readme.md", "README.rst", "README.txt", "README"]
        branches = ["main", "master"]

        async def _fetch() -> bytes:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                for branch in branches:
                    for readme in readme_variants:
                        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{readme}"
                        try:
                            response = await client.get(url)
                            if response.status_code == 200:
                                logger.info(
                                    "Found GitHub README: %s/%s (%s, %s)", owner, repo, branch, readme
                                )
                                return response.content
                        except httpx.HTTPError:
                            continue
            raise GitHubReadmeNotFoundError(f"No README found for {owner}/{repo}")

        readme_bytes = asyncio.run(_fetch())
        return ConversionInput(
            content=readme_bytes,
            content_type=ContentType.HTML,
            metadata={"source_url": f"https://github.com/{owner}/{repo}"},
        )
