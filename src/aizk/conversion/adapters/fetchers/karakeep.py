"""KaraKeep bookmark resolver adapter implementing the RefResolver protocol.

Resolves a KarakeepBookmarkRef to a more specific SourceRef by fetching the
bookmark from KaraKeep and applying a 7-step content-precedence chain.
"""

from __future__ import annotations

import os
from typing import ClassVar

from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    InlineHtmlRef,
    KarakeepBookmarkRef,
    SourceRef,
    UrlRef,
)
from aizk.conversion.utilities.arxiv_utils import get_arxiv_id
from aizk.conversion.utilities.bookmark_utils import (
    detect_source_type,
    fetch_karakeep_bookmark,
    get_bookmark_asset_id,
    get_bookmark_html_content,
    get_bookmark_source_url,
    get_bookmark_text_content,
    is_pdf_asset,
    is_precrawled_archive_asset,
)
from aizk.conversion.utilities.github_utils import parse_github_owner_repo

# BookmarkContentUnavailableError lives in workers.fetcher (inherits from both
# FetchError and bookmark_utils.BookmarkContentError).  The workers.fetcher
# module's lazy __getattr__ shim only fires when *adapter class names* are
# looked up, so importing BookmarkContentUnavailableError at module load time
# does NOT create a circular import.
from aizk.conversion.workers.fetcher import BookmarkContentUnavailableError


class KarakeepBookmarkResolver:
    """RefResolver that resolves a KarakeepBookmarkRef to a typed SourceRef.

    Applies a 7-step content-precedence chain against the fetched bookmark and
    returns the most specific SourceRef variant possible.
    """

    resolves_to: ClassVar[frozenset[str]] = frozenset({"arxiv", "github_readme", "url", "inline_html"})

    def resolve(self, ref: SourceRef) -> SourceRef:
        """Fetch the KaraKeep bookmark and resolve it to a typed SourceRef.

        Args:
            ref: A KarakeepBookmarkRef to resolve.

        Returns:
            The resolved SourceRef (ArxivRef, GithubReadmeRef, UrlRef, or InlineHtmlRef).

        Raises:
            BookmarkContentUnavailableError: If the bookmark cannot be fetched or has no
                usable content.
        """
        assert isinstance(ref, KarakeepBookmarkRef), f"Expected KarakeepBookmarkRef, got {type(ref)}"

        bookmark = fetch_karakeep_bookmark(ref.bookmark_id)
        if bookmark is None:
            raise BookmarkContentUnavailableError(
                f"KaraKeep bookmark {ref.bookmark_id!r} could not be fetched or does not exist"
            )

        # Extract source URL if available (used across multiple steps).
        try:
            source_url: str | None = get_bookmark_source_url(bookmark)
        except Exception:
            source_url = None

        karakeep_base_url = os.environ.get("KARAKEEP_BASE_URL", "").rstrip("/")

        # Step 1 — arXiv bookmark
        if source_url and detect_source_type(source_url) == "arxiv":
            arxiv_id = get_arxiv_id(source_url)
            if is_pdf_asset(bookmark):
                asset_id = get_bookmark_asset_id(bookmark)
                arxiv_pdf_url: str | None = f"{karakeep_base_url}/api/v1/assets/{asset_id}"
            else:
                arxiv_pdf_url = None
            return ArxivRef(arxiv_id=arxiv_id, arxiv_pdf_url=arxiv_pdf_url)

        # Step 2 — GitHub bookmark
        if source_url and detect_source_type(source_url) == "github":
            owner, repo = parse_github_owner_repo(source_url)
            return GithubReadmeRef(owner=owner, repo=repo)

        # Step 3 — non-arxiv/non-github PDF asset
        if is_pdf_asset(bookmark):
            asset_id = get_bookmark_asset_id(bookmark)
            asset_url = f"{karakeep_base_url}/api/v1/assets/{asset_id}"
            return UrlRef(url=asset_url)

        # Step 4 — precrawled archive asset (HTML pipeline)
        if is_precrawled_archive_asset(bookmark):
            asset_id = get_bookmark_asset_id(bookmark)
            asset_url = f"{karakeep_base_url}/api/v1/assets/{asset_id}"
            return UrlRef(url=asset_url)

        # Step 5 — HTML content present
        html_content = get_bookmark_html_content(bookmark)
        if html_content:
            if source_url:
                return UrlRef(url=source_url)
            html_bytes = html_content.encode("utf-8")
            return InlineHtmlRef(body=html_bytes)

        # Step 6 — text content only
        text_content = get_bookmark_text_content(bookmark)
        if text_content:
            html = f"<html><body><pre>{text_content}</pre></body></html>"
            return InlineHtmlRef(body=html.encode("utf-8"))

        # Step 7 — no usable content
        raise BookmarkContentUnavailableError(
            f"KaraKeep bookmark {ref.bookmark_id!r} has no usable content (no arxiv, github, PDF, "
            "archive, HTML, or text)"
        )


__all__ = ["KarakeepBookmarkResolver"]
