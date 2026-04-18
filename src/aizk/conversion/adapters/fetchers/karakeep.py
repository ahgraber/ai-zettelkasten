"""KarakeepBookmarkResolver adapter implementing the RefResolver protocol."""

from __future__ import annotations

import html as _html
import logging
from typing import ClassVar

from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    InlineHtmlRef,
    SourceRefVariant,
    UrlRef,
)
from aizk.conversion.utilities.config import ConversionConfig

logger = logging.getLogger(__name__)


class KarakeepBookmarkResolver:
    """RefResolver that maps a KarakeepBookmarkRef to a more specific SourceRef.

    Preserves the 7-step resolution precedence from the legacy orchestrator:

    1. arxiv source_type  → ArxivRef (with karakeep_asset_url if PDF asset present)
    2. github source_type → GithubReadmeRef (or UrlRef for GitHub Pages)
    3. PDF asset          → UrlRef pointing at KaraKeep asset endpoint
    4. Precrawled archive → UrlRef pointing at KaraKeep asset endpoint
    5. HTML content       → UrlRef pointing at source URL
    6. Text content       → InlineHtmlRef with HTML-wrapped text
    7. No content         → BookmarkContentUnavailableError (permanent failure)
    """

    resolves_to: ClassVar[frozenset[str]] = frozenset(
        {"arxiv", "github_readme", "url", "inline_html"}
    )
    api_submittable: ClassVar[bool] = True

    def __init__(self, config: ConversionConfig) -> None:
        self._config = config

    def _karakeep_asset_url(self, asset_id: str) -> str:
        """Construct the KaraKeep REST URL for a given asset ID."""
        base = self._config.fetcher.karakeep.base_url.rstrip("/")
        return f"{base}/api/v1/assets/{asset_id}"

    def resolve(self, ref: SourceRefVariant) -> SourceRefVariant:
        """Fetch the bookmark from KaraKeep and refine ``ref`` using it.

        Most callers hit this method. The parent process can also call
        :meth:`refine_from_bookmark` directly to share an already-fetched
        bookmark between the enrichment step and the resolution step — see
        ``workers.orchestrator._enrich_source_for_job``.
        """
        from aizk.conversion.utilities.bookmark_utils import fetch_karakeep_bookmark
        from aizk.conversion.workers.fetcher import FetchError

        karakeep = self._config.fetcher.karakeep
        bookmark = fetch_karakeep_bookmark(
            ref.bookmark_id,  # type: ignore[union-attr]
            base_url=karakeep.base_url or None,
            api_key=karakeep.api_key or None,
        )
        if bookmark is None:
            raise FetchError(f"Bookmark {ref.bookmark_id} not found in KaraKeep")  # type: ignore[union-attr]
        return self.refine_from_bookmark(ref, bookmark)

    def refine_from_bookmark(self, ref: SourceRefVariant, bookmark: object) -> SourceRefVariant:
        """Refine ``ref`` using an already-fetched *bookmark*.

        Public seam so a caller that already holds the KaraKeep bookmark
        (e.g. the worker's parent-side enrichment step) can run the
        resolution precedence without a second RPC.
        """
        from aizk.conversion.utilities.bookmark_utils import (
            BookmarkContentError,
            BookmarkContentUnavailableError,
            detect_source_type,
            get_bookmark_asset_id,
            get_bookmark_html_content,
            get_bookmark_source_url,
            get_bookmark_text_content,
            is_pdf_asset,
            is_precrawled_archive_asset,
        )
        from aizk.conversion.utilities.arxiv_utils import get_arxiv_id
        from aizk.conversion.utilities.github_utils import (
            is_github_pages_url,
            parse_github_owner_repo,
        )

        try:
            source_url: str | None = get_bookmark_source_url(bookmark)
        except BookmarkContentError:
            source_url = None

        source_type = detect_source_type(source_url) if source_url else "other"

        # Step 1: arxiv
        if source_type == "arxiv":
            arxiv_id = get_arxiv_id(source_url)  # type: ignore[arg-type]
            karakeep_asset_url: str | None = None
            if is_pdf_asset(bookmark):
                asset_id = get_bookmark_asset_id(bookmark)
                if asset_id:
                    karakeep_asset_url = self._karakeep_asset_url(asset_id)
            return ArxivRef(arxiv_id=arxiv_id, karakeep_asset_url=karakeep_asset_url)

        # Step 2: github
        if source_type == "github":
            if source_url and is_github_pages_url(source_url):
                return UrlRef(url=source_url)
            if source_url:
                try:
                    owner, repo = parse_github_owner_repo(source_url)
                    return GithubReadmeRef(owner=owner, repo=repo)
                except ValueError:
                    return UrlRef(url=source_url)

        # Step 3: PDF asset (non-arxiv)
        if is_pdf_asset(bookmark):
            asset_id = get_bookmark_asset_id(bookmark)
            if asset_id:
                return UrlRef(url=self._karakeep_asset_url(asset_id))

        # Step 4: precrawled archive
        if is_precrawled_archive_asset(bookmark):
            asset_id = get_bookmark_asset_id(bookmark)
            if asset_id:
                return UrlRef(url=self._karakeep_asset_url(asset_id))

        # Step 5: HTML content — embed inline (preserves legacy: uses cached crawler bytes directly)
        html_content = get_bookmark_html_content(bookmark)
        if html_content and html_content.strip():
            return InlineHtmlRef(body=html_content.encode("utf-8"))

        # Step 6: text content — embed inline, wrapped in minimal HTML
        text_content = get_bookmark_text_content(bookmark)
        if text_content and text_content.strip():
            wrapped = f"<html><body><pre>{_html.escape(text_content)}</pre></body></html>"
            return InlineHtmlRef(body=wrapped.encode("utf-8"))

        # Step 7: no usable content
        raise BookmarkContentUnavailableError(
            f"Bookmark {ref.bookmark_id} has no usable content"  # type: ignore[union-attr]
        )
