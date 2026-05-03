"""Unit tests for KarakeepBookmarkResolver adapter.

All tests are hermetic: no .env, no real network, no real KaraKeep calls.
"""

from __future__ import annotations

import pytest

from aizk.conversion.adapters.fetchers.karakeep import KarakeepBookmarkResolver
from aizk.conversion.core.errors import BookmarkContentUnavailableError
from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    InlineHtmlRef,
    KarakeepBookmarkRef,
    UrlRef,
)
from aizk.conversion.core.types import ContentType, SourceMetadata
from aizk.conversion.utilities.config import KarakeepFetcherConfig
from karakeep_client.models import Bookmark

_DEFAULT_CFG = KarakeepFetcherConfig(_env_file=None, base_url="", api_key="")

# ---------------------------------------------------------------------------
# Bookmark fixture helpers
# ---------------------------------------------------------------------------

_BASE_BOOKMARK = {
    "id": "bm-test",
    "createdAt": "2025-01-01T00:00:00.000Z",
    "modifiedAt": "2025-01-01T00:00:00.000Z",
    "title": None,
    "archived": False,
    "favourited": False,
    "taggingStatus": "success",
    "summarizationStatus": "success",
    "note": None,
    "summary": None,
    "tags": [],
    "assets": [],
}

_BASE_LINK_CONTENT = {
    "type": "link",
    "title": None,
    "description": None,
    "imageUrl": None,
    "imageAssetId": None,
    "screenshotAssetId": None,
    "fullPageArchiveAssetId": None,
    "precrawledArchiveAssetId": None,
    "videoAssetId": None,
    "favicon": None,
    "htmlContent": None,
    "contentAssetId": None,
    "crawledAt": None,
    "author": None,
    "publisher": None,
    "datePublished": None,
    "dateModified": None,
}


def _link_bookmark(url: str, **overrides) -> Bookmark:
    content = {**_BASE_LINK_CONTENT, "url": url, **overrides}
    return Bookmark.model_validate({**_BASE_BOOKMARK, "content": content})


def _pdf_asset_bookmark(source_url: str, asset_id: str) -> Bookmark:
    return Bookmark.model_validate(
        {
            **_BASE_BOOKMARK,
            "content": {
                "type": "asset",
                "assetType": "pdf",
                "assetId": asset_id,
                "fileName": "paper.pdf",
                "sourceUrl": source_url,
                "size": 1000.0,
                "content": None,
            },
            "assets": [{"id": asset_id, "assetType": "bookmarkAsset"}],
        }
    )


def _text_bookmark(text: str) -> Bookmark:
    return Bookmark.model_validate(
        {
            **_BASE_BOOKMARK,
            "content": {
                "type": "text",
                "text": text,
                "sourceUrl": None,
            },
        }
    )


# ---------------------------------------------------------------------------
# Class-level structural tests
# ---------------------------------------------------------------------------


def test_karakeep_resolver_resolves_to_frozenset_of_four_kinds():
    assert KarakeepBookmarkResolver.resolves_to == frozenset({"arxiv", "github_readme", "url", "inline_html"})


def test_karakeep_resolver_satisfies_refresolver_protocol_structurally():
    resolver = KarakeepBookmarkResolver(_DEFAULT_CFG)
    assert hasattr(resolver, "resolve")
    assert hasattr(KarakeepBookmarkResolver, "resolves_to")
    assert callable(resolver.resolve)
    # Runtime-checkable protocol check
    from aizk.conversion.core.protocols import RefResolver

    assert isinstance(resolver, RefResolver)


# ---------------------------------------------------------------------------
# Step 1 — arXiv bookmark without PDF asset
# ---------------------------------------------------------------------------


def test_karakeep_resolver_arxiv_bookmark_returns_arxiv_ref(monkeypatch):
    bookmark = _link_bookmark("https://arxiv.org/abs/2301.12345")
    ref = KarakeepBookmarkRef(bookmark_id="bm-arxiv")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "arxiv",
    )

    resolver = KarakeepBookmarkResolver(_DEFAULT_CFG)
    result, _ = resolver.resolve(ref)

    assert isinstance(result, ArxivRef)
    assert result.arxiv_id == "2301.12345"
    assert result.arxiv_pdf_url is None


# ---------------------------------------------------------------------------
# Step 1 — arXiv bookmark WITH PDF asset → arxiv_pdf_url set
# ---------------------------------------------------------------------------


def test_karakeep_resolver_arxiv_pdf_asset_sets_arxiv_pdf_url(monkeypatch):
    """ArxivRef.arxiv_pdf_url should point to the KaraKeep asset URL."""
    # Use a PDF asset bookmark whose source_url is an arXiv URL
    bookmark = _pdf_asset_bookmark(
        source_url="https://arxiv.org/abs/2301.12345",
        asset_id="asset-abc",
    )
    ref = KarakeepBookmarkRef(bookmark_id="bm-arxiv-pdf")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "arxiv",
    )

    cfg = KarakeepFetcherConfig(_env_file=None, base_url="https://karakeep.example.com", api_key="")
    resolver = KarakeepBookmarkResolver(cfg)
    result, _ = resolver.resolve(ref)

    assert isinstance(result, ArxivRef)
    assert result.arxiv_id == "2301.12345"
    assert result.arxiv_pdf_url == "https://karakeep.example.com/api/v1/assets/asset-abc"


# ---------------------------------------------------------------------------
# Step 2 — GitHub bookmark
# ---------------------------------------------------------------------------


def test_karakeep_resolver_github_bookmark_returns_github_readme_ref(monkeypatch):
    bookmark = _link_bookmark("https://github.com/owner/repo")
    ref = KarakeepBookmarkRef(bookmark_id="bm-github")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "github",
    )

    resolver = KarakeepBookmarkResolver(_DEFAULT_CFG)
    result, _ = resolver.resolve(ref)

    assert isinstance(result, GithubReadmeRef)
    assert result.owner == "owner"
    assert result.repo == "repo"


# ---------------------------------------------------------------------------
# Step 3 — non-arxiv, non-github PDF asset
# ---------------------------------------------------------------------------


def test_karakeep_resolver_pdf_asset_bookmark_returns_url_ref(monkeypatch):
    bookmark = _pdf_asset_bookmark(
        source_url="https://example.com/paper.pdf",
        asset_id="pdf-asset-1",
    )
    ref = KarakeepBookmarkRef(bookmark_id="bm-pdf")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "other",
    )

    cfg = KarakeepFetcherConfig(_env_file=None, base_url="https://karakeep.example.com", api_key="")
    resolver = KarakeepBookmarkResolver(cfg)
    result, _ = resolver.resolve(ref)

    assert isinstance(result, UrlRef)
    assert result.url == "https://karakeep.example.com/api/v1/assets/pdf-asset-1"
    assert result.content_type_hint is ContentType.PDF


# ---------------------------------------------------------------------------
# Step 5 — HTML content with source URL → live URL
# ---------------------------------------------------------------------------


def test_karakeep_resolver_html_content_returns_url_ref(monkeypatch):
    bookmark = _link_bookmark(
        "https://example.com/page",
        htmlContent="<html><body>Hello</body></html>",
    )
    ref = KarakeepBookmarkRef(bookmark_id="bm-html")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "other",
    )

    resolver = KarakeepBookmarkResolver(_DEFAULT_CFG)
    result, _ = resolver.resolve(ref)

    assert isinstance(result, UrlRef)
    assert "example.com" in result.url


def test_karakeep_resolver_step5_emits_urlref_for_deny_set_source_url(monkeypatch):
    """Step 5 with a deny-set source_url succeeds and emits a UrlRef.

    Per `network-egress-policy/design.md` § "Defer egress validation to fetch
    time only", the resolver no longer rejects deny-set destinations at
    construction.  The load-bearing rejection happens when the downstream
    fetcher dispatches via ``egress_fetch_bytes`` (covered by
    ``tests/conversion/unit/utilities/test_egress.py`` and the integration
    suite).  This test pins that the resolver itself does not classify the
    destination — it only constructs the typed ref.
    """
    bookmark = _link_bookmark(
        "http://169.254.169.254/latest/meta-data/iam/",
        htmlContent="<html><body>x</body></html>",
    )
    ref = KarakeepBookmarkRef(bookmark_id="bm-deny")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "other",
    )

    resolver = KarakeepBookmarkResolver(_DEFAULT_CFG)
    resolved, _ = resolver.resolve(ref)

    assert isinstance(resolved, UrlRef)
    # `normalize_url` strips the trailing slash; no DNS, no classification.
    assert resolved.url == "http://169.254.169.254/latest/meta-data/iam"


# ---------------------------------------------------------------------------
# Step 6 — text-only content → InlineHtmlRef
# ---------------------------------------------------------------------------


def test_karakeep_resolver_text_only_returns_inline_html_ref(monkeypatch):
    bookmark = _text_bookmark("hello world")
    ref = KarakeepBookmarkRef(bookmark_id="bm-text")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )

    resolver = KarakeepBookmarkResolver(_DEFAULT_CFG)
    result, _ = resolver.resolve(ref)

    assert isinstance(result, InlineHtmlRef)
    assert result.body == b"<html><body><pre>hello world</pre></body></html>"


# ---------------------------------------------------------------------------
# Step 7 — no usable content → BookmarkContentUnavailableError
# ---------------------------------------------------------------------------


def test_karakeep_resolver_empty_bookmark_raises_content_unavailable(monkeypatch):
    # A link bookmark with no HTML content, no PDF asset, not arxiv, not github
    bookmark = _link_bookmark("https://example.com/nothing")
    ref = KarakeepBookmarkRef(bookmark_id="bm-empty")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "other",
    )

    resolver = KarakeepBookmarkResolver(_DEFAULT_CFG)
    with pytest.raises(BookmarkContentUnavailableError):
        resolver.resolve(ref)


# ---------------------------------------------------------------------------
# fetch returns None → BookmarkContentUnavailableError
# ---------------------------------------------------------------------------


def test_karakeep_resolver_missing_bookmark_raises_content_unavailable(monkeypatch):
    ref = KarakeepBookmarkRef(bookmark_id="bm-missing")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: None,
    )

    resolver = KarakeepBookmarkResolver(_DEFAULT_CFG)
    with pytest.raises(BookmarkContentUnavailableError):
        resolver.resolve(ref)


# ---------------------------------------------------------------------------
# SourceMetadata fields — returned alongside the resolved ref
# ---------------------------------------------------------------------------


def _link_bookmark_with_title(url: str, title: str | None = None, **overrides):
    """Bookmark with an explicit title field for metadata tests."""
    base = {
        **_BASE_BOOKMARK,
        "title": title,
        "content": {**_BASE_LINK_CONTENT, "url": url, **overrides},
    }
    return Bookmark.model_validate(base)


def test_karakeep_resolver_arxiv_meta_source_url(monkeypatch):
    """ArXiv branch: source_url and document_base_url come from the bookmark URL."""
    bookmark = _link_bookmark_with_title("https://arxiv.org/abs/2301.12345", title="Attention Is All You Need")
    ref = KarakeepBookmarkRef(bookmark_id="bm-arxiv-meta")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "arxiv",
    )

    _, meta = KarakeepBookmarkResolver(_DEFAULT_CFG).resolve(ref)

    assert isinstance(meta, SourceMetadata)
    assert meta.source_url == "https://arxiv.org/abs/2301.12345"
    assert meta.document_base_url == "https://arxiv.org/abs/2301.12345"
    assert meta.resolver_title == "Attention Is All You Need"
    assert meta.normalized_url is not None
    assert "arxiv.org" in meta.normalized_url


def test_karakeep_resolver_github_meta_source_url(monkeypatch):
    """GitHub branch: source_url, document_base_url, and resolver_title populated."""
    bookmark = _link_bookmark_with_title("https://github.com/owner/repo", title="owner/repo")
    ref = KarakeepBookmarkRef(bookmark_id="bm-github-meta")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "github",
    )

    _, meta = KarakeepBookmarkResolver(_DEFAULT_CFG).resolve(ref)

    assert meta.source_url == "https://github.com/owner/repo"
    assert meta.document_base_url == "https://github.com/owner/repo"
    assert meta.resolver_title == "owner/repo"
    assert meta.normalized_url is not None


def test_karakeep_resolver_pdf_asset_meta_source_url(monkeypatch):
    """PDF-asset branch: source_url comes from the bookmark's sourceUrl."""
    bookmark = _pdf_asset_bookmark(source_url="https://example.com/paper.pdf", asset_id="pdf-asset-2")
    ref = KarakeepBookmarkRef(bookmark_id="bm-pdf-meta")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "other",
    )

    _, meta = KarakeepBookmarkResolver(_DEFAULT_CFG).resolve(ref)

    assert meta.source_url == "https://example.com/paper.pdf"
    assert meta.document_base_url == "https://example.com/paper.pdf"
    assert meta.normalized_url is not None
    assert "example.com" in meta.normalized_url


def test_karakeep_resolver_inline_html_meta_null_source_url(monkeypatch):
    """Inline HTML branch (no source URL): source_url and document_base_url are None."""
    bookmark = _link_bookmark("https://placeholder.example.com", htmlContent="<html><body>hello</body></html>")
    ref = KarakeepBookmarkRef(bookmark_id="bm-inline-meta")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    # Simulate a bookmark where the source URL is not extractable
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.get_bookmark_source_url",
        lambda _: None,
    )

    _, meta = KarakeepBookmarkResolver(_DEFAULT_CFG).resolve(ref)

    assert meta.source_url is None
    assert meta.document_base_url is None
    assert meta.normalized_url is None


def test_karakeep_resolver_normalize_url_failure_yields_none_and_logs_debug(monkeypatch, caplog):
    """When normalize_url raises on the resolver path, normalized_url is None and a debug log is emitted."""
    import logging

    bookmark = _link_bookmark("https://example.com/page", htmlContent="<html/>")
    ref = KarakeepBookmarkRef(bookmark_id="bm-norm-fail")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "other",
    )

    def _raising_normalize(url):
        raise ValueError(f"malformed url: {url}")

    monkeypatch.setattr("aizk.utilities.url_utils.normalize_url", _raising_normalize)

    with caplog.at_level(logging.DEBUG, logger="aizk.conversion.adapters.fetchers.karakeep"):
        _, meta = KarakeepBookmarkResolver(_DEFAULT_CFG).resolve(ref)

    assert meta.normalized_url is None
    # Resolver still returns the source URL it observed; only normalization failed.
    assert meta.source_url == "https://example.com/page"
    assert any("normalize_url failed" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG), (
        "expected a DEBUG log line announcing normalize_url failure"
    )


def test_karakeep_resolver_null_title_yields_none_resolver_title(monkeypatch):
    """Bookmark with title=None produces resolver_title=None."""
    bookmark = _link_bookmark_with_title("https://example.com/page", title=None)
    ref = KarakeepBookmarkRef(bookmark_id="bm-no-title")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.fetch_karakeep_bookmark",
        lambda _: bookmark,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.detect_source_type",
        lambda url: "other",
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.karakeep.get_bookmark_html_content",
        lambda _: "<html/>",
    )

    _, meta = KarakeepBookmarkResolver(_DEFAULT_CFG).resolve(ref)

    assert meta.resolver_title is None
