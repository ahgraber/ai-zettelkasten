"""Tests for fetcher adapters: KarakeepBookmarkResolver, ArxivFetcher, InlineContentFetcher.

All tests that exercise resolve() / fetch() inject mock stubs for the heavy
utility and worker modules that are unavailable in the unit-test venv
(karakeep_client, defusedxml, httpx, validators, etc.).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from aizk.conversion.adapters.fetchers.inline import InlineContentFetcher
from aizk.conversion.adapters.fetchers.karakeep import KarakeepBookmarkResolver
from aizk.conversion.adapters.fetchers.singlefile import SingleFileFetcher
from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    InlineHtmlRef,
    KarakeepBookmarkRef,
    SingleFileRef,
    UrlRef,
)
from aizk.conversion.core.types import ContentType, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig


def _resolver_config(base_url: str = "https://karakeep.example.com") -> ConversionConfig:
    return ConversionConfig(
        _env_file=None,
        fetcher={"karakeep": {"base_url": base_url, "api_key": "test-key"}},
    )


# ---------------------------------------------------------------------------
# Local exception stubs (structurally match bookmark_utils / fetcher.py classes)
# ---------------------------------------------------------------------------

class _BookmarkContentError(ValueError):
    error_code = "karakeep_bookmark_missing_contents"
    retryable = False


class _BookmarkContentUnavailableError(_BookmarkContentError):
    retryable = False


class _FetchError(Exception):
    error_code = "fetch_error"
    retryable = True


_ArxivPdfFetchError = type("ArxivPdfFetchError", (_FetchError,), {"error_code": "arxiv_pdf_fetch_failed"})
_GitHubReadmeNotFoundError = type(
    "GitHubReadmeNotFoundError", (_FetchError,), {"error_code": "github_readme_not_found", "retryable": False}
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def heavy_mocks(monkeypatch):
    """Inject mock modules for all heavy deps that karakeep.py / arxiv.py import lazily."""
    bm_mod = MagicMock(name="bookmark_utils")
    bm_mod.BookmarkContentError = _BookmarkContentError
    bm_mod.BookmarkContentUnavailableError = _BookmarkContentUnavailableError
    bm_mod.fetch_karakeep_bookmark.return_value = MagicMock()
    bm_mod.get_bookmark_source_url.return_value = "https://example.com"
    bm_mod.detect_source_type.return_value = "other"
    bm_mod.get_bookmark_html_content.return_value = None
    bm_mod.get_bookmark_text_content.return_value = None
    bm_mod.is_pdf_asset.return_value = False
    bm_mod.is_precrawled_archive_asset.return_value = False
    bm_mod.get_bookmark_asset_id.return_value = None

    arxiv_mod = MagicMock(name="arxiv_utils")
    arxiv_mod.get_arxiv_id.return_value = "2301.00000"

    gh_mod = MagicMock(name="github_utils")
    gh_mod.is_github_pages_url.return_value = False
    gh_mod.parse_github_owner_repo.return_value = ("owner", "repo")

    fetcher_mod = MagicMock(name="fetcher")
    fetcher_mod.FetchError = _FetchError
    fetcher_mod.ArxivPdfFetchError = _ArxivPdfFetchError
    fetcher_mod.GitHubReadmeNotFoundError = _GitHubReadmeNotFoundError

    for mod_name, mock in [
        ("aizk.conversion.utilities.bookmark_utils", bm_mod),
        ("aizk.conversion.utilities.arxiv_utils", arxiv_mod),
        ("aizk.conversion.utilities.github_utils", gh_mod),
        ("aizk.conversion.workers.fetcher", fetcher_mod),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mock)

    return {"bm": bm_mod, "arxiv": arxiv_mod, "gh": gh_mod, "fetcher": fetcher_mod}


# ---------------------------------------------------------------------------
# KarakeepBookmarkResolver — class attributes (no heavy deps)
# ---------------------------------------------------------------------------

def test_karakeep_resolver_resolves_to_is_frozenset():
    assert isinstance(KarakeepBookmarkResolver.resolves_to, frozenset)


def test_karakeep_resolver_resolves_to_contains_expected_kinds():
    assert KarakeepBookmarkResolver.resolves_to == {"arxiv", "github_readme", "url", "inline_html"}


def test_karakeep_resolver_class_attrs_inspectable_without_instantiation():
    assert hasattr(KarakeepBookmarkResolver, "resolves_to")


# ---------------------------------------------------------------------------
# KarakeepBookmarkResolver — resolution precedence
# ---------------------------------------------------------------------------

def test_arxiv_bookmark_returns_arxiv_ref(heavy_mocks, monkeypatch):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://arxiv.org/abs/2301.12345"
    mocks["bm"].detect_source_type.return_value = "arxiv"
    mocks["bm"].is_pdf_asset.return_value = False
    mocks["arxiv"].get_arxiv_id.return_value = "2301.12345"

    result = KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-1"))

    assert isinstance(result, ArxivRef)
    assert result.arxiv_id == "2301.12345"
    assert result.karakeep_asset_url is None


def test_arxiv_bookmark_with_pdf_asset_sets_karakeep_asset_url(heavy_mocks, monkeypatch):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://arxiv.org/abs/2301.12345"
    mocks["bm"].detect_source_type.return_value = "arxiv"
    mocks["bm"].is_pdf_asset.return_value = True
    mocks["bm"].get_bookmark_asset_id.return_value = "asset-xyz"
    mocks["arxiv"].get_arxiv_id.return_value = "2301.12345"

    result = KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-1"))

    assert isinstance(result, ArxivRef)
    assert result.karakeep_asset_url == "https://karakeep.example.com/api/v1/assets/asset-xyz"


def test_github_bookmark_returns_github_readme_ref(heavy_mocks):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://github.com/owner/repo"
    mocks["bm"].detect_source_type.return_value = "github"
    mocks["gh"].is_github_pages_url.return_value = False
    mocks["gh"].parse_github_owner_repo.return_value = ("owner", "repo")

    result = KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-2"))

    assert isinstance(result, GithubReadmeRef)
    assert result.owner == "owner"
    assert result.repo == "repo"


def test_github_pages_bookmark_returns_url_ref(heavy_mocks):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://owner.github.io/site"
    mocks["bm"].detect_source_type.return_value = "github"
    mocks["gh"].is_github_pages_url.return_value = True

    result = KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-3"))

    assert isinstance(result, UrlRef)
    assert result.url == "https://owner.github.io/site"


def test_pdf_asset_bookmark_returns_url_ref(heavy_mocks):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://example.com/doc"
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].is_pdf_asset.return_value = True
    mocks["bm"].get_bookmark_asset_id.return_value = "pdf-asset-1"

    result = KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-4"))

    assert isinstance(result, UrlRef)
    assert "pdf-asset-1" in result.url


def test_precrawled_archive_returns_url_ref(heavy_mocks):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://example.com/page"
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].is_pdf_asset.return_value = False
    mocks["bm"].is_precrawled_archive_asset.return_value = True
    mocks["bm"].get_bookmark_asset_id.return_value = "archive-1"

    result = KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-5"))

    assert isinstance(result, UrlRef)
    assert "archive-1" in result.url


def test_html_content_bookmark_returns_inline_html_ref(heavy_mocks):
    # Legacy behavior: embed cached HTML bytes inline (no extra fetch from source_url).
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://example.com/article"
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].get_bookmark_html_content.return_value = "<html><body>content</body></html>"

    result = KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-6"))

    assert isinstance(result, InlineHtmlRef)
    assert b"content" in result.body


def test_html_content_without_source_url_returns_inline_html_ref(heavy_mocks):
    # Regression: must not silently fall through when source_url is absent.
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.side_effect = _BookmarkContentError("no url")
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].get_bookmark_html_content.return_value = "<html><body>no-url content</body></html>"

    result = KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-6b"))

    assert isinstance(result, InlineHtmlRef)
    assert b"no-url content" in result.body


def test_text_only_bookmark_returns_inline_html_ref(heavy_mocks):
    mocks = heavy_mocks
    # No source_url — raises BookmarkContentError so source_url becomes None
    mocks["bm"].get_bookmark_source_url.side_effect = _BookmarkContentError("no url")
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].get_bookmark_html_content.return_value = None
    mocks["bm"].get_bookmark_text_content.return_value = "some plain text"

    result = KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-7"))

    assert isinstance(result, InlineHtmlRef)
    assert b"some plain text" in result.body
    assert b"<pre>" in result.body


def test_empty_bookmark_raises_error(heavy_mocks):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.side_effect = _BookmarkContentError("no url")
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].get_bookmark_html_content.return_value = None
    mocks["bm"].get_bookmark_text_content.return_value = None

    with pytest.raises(_BookmarkContentUnavailableError):
        KarakeepBookmarkResolver(config=_resolver_config()).resolve(KarakeepBookmarkRef(bookmark_id="bk-8"))


# ---------------------------------------------------------------------------
# refine_from_bookmark — RPC-free resolution path (F4, #29)
# ---------------------------------------------------------------------------


def test_refine_from_bookmark_does_not_call_fetch_karakeep_bookmark(heavy_mocks):
    """Pre-fetched-bookmark seam is the whole point: no RPC on this path.

    Parent-side enrichment fetches the bookmark once, then calls
    ``refine_from_bookmark`` to run the 7-step precedence without a second
    RPC. The child's orchestrator then receives the already-refined ref
    and dispatches straight to a terminal content fetcher.
    """
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://arxiv.org/abs/2301.12345"
    mocks["bm"].detect_source_type.return_value = "arxiv"
    mocks["bm"].is_pdf_asset.return_value = False
    mocks["arxiv"].get_arxiv_id.return_value = "2301.12345"

    # fetch_karakeep_bookmark is the RPC we must NOT make on this path.
    mocks["bm"].fetch_karakeep_bookmark.reset_mock()

    resolver = KarakeepBookmarkResolver(config=_resolver_config())
    prefetched_bookmark = MagicMock(name="prefetched_bookmark")

    result = resolver.refine_from_bookmark(
        KarakeepBookmarkRef(bookmark_id="bk-29"),
        prefetched_bookmark,
    )

    assert isinstance(result, ArxivRef)
    assert result.arxiv_id == "2301.12345"
    mocks["bm"].fetch_karakeep_bookmark.assert_not_called()


def test_resolve_delegates_post_fetch_logic_to_refine_from_bookmark(heavy_mocks):
    """``resolve`` and ``refine_from_bookmark`` produce identical outputs for
    a given bookmark — one goes through the RPC, the other skips it, but
    the refinement logic is the same.
    """
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://github.com/owner/repo"
    mocks["bm"].detect_source_type.return_value = "github"
    mocks["gh"].is_github_pages_url.return_value = False
    mocks["gh"].parse_github_owner_repo.return_value = ("owner", "repo")

    prefetched_bookmark = MagicMock(name="prefetched_bookmark")
    mocks["bm"].fetch_karakeep_bookmark.return_value = prefetched_bookmark

    resolver = KarakeepBookmarkResolver(config=_resolver_config())
    ref = KarakeepBookmarkRef(bookmark_id="bk-29b")

    via_resolve = resolver.resolve(ref)
    via_refine = resolver.refine_from_bookmark(ref, prefetched_bookmark)

    assert type(via_resolve) is type(via_refine)
    assert via_resolve == via_refine


# ---------------------------------------------------------------------------
# ArxivFetcher — PDF source precedence
# ---------------------------------------------------------------------------

@pytest.fixture()
def httpx_mock(monkeypatch):
    """Inject a mock httpx module into sys.modules with a pre-wired Client."""
    mock_httpx = MagicMock(name="httpx")

    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    mock_httpx.Client.return_value = client

    monkeypatch.setitem(sys.modules, "httpx", mock_httpx)
    return mock_httpx


def test_arxiv_fetcher_prefers_karakeep_asset_url(httpx_mock, heavy_mocks):
    """karakeep_asset_url is fetched first when present."""
    mock_response = MagicMock()
    mock_response.content = b"pdf-bytes-from-karakeep"
    mock_response.raise_for_status = MagicMock()

    httpx_mock.Client.return_value.get.return_value = mock_response
    heavy_mocks["arxiv"].ArxivClient = MagicMock()

    from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher
    ref = ArxivRef(arxiv_id="2301.12345", karakeep_asset_url="https://karakeep.example.com/api/v1/assets/a1")
    result = ArxivFetcher().fetch(ref)

    assert result.content == b"pdf-bytes-from-karakeep"
    assert result.content_type is ContentType.PDF
    httpx_mock.Client.return_value.get.assert_called_once_with(
        "https://karakeep.example.com/api/v1/assets/a1"
    )


def test_arxiv_fetcher_falls_back_to_arxiv_pdf_url(httpx_mock, heavy_mocks):
    """arxiv_pdf_url is used when karakeep_asset_url is absent."""
    mock_response = MagicMock()
    mock_response.content = b"pdf-bytes-from-url"
    mock_response.raise_for_status = MagicMock()

    httpx_mock.Client.return_value.get.return_value = mock_response
    heavy_mocks["arxiv"].ArxivClient = MagicMock()

    from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher
    ref = ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url="https://export.arxiv.org/pdf/2301.12345")
    result = ArxivFetcher().fetch(ref)

    assert result.content == b"pdf-bytes-from-url"
    assert result.content_type is ContentType.PDF
    httpx_mock.Client.return_value.get.assert_called_once_with(
        "https://export.arxiv.org/pdf/2301.12345"
    )


def test_arxiv_fetcher_constructs_url_from_arxiv_id(httpx_mock, heavy_mocks, monkeypatch):
    """When no asset or explicit URL, ArxivClient is used to download by ID."""
    import asyncio

    # ArxivClient is imported from the already-mocked arxiv_utils module
    class _FakeArxivClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def download_paper_pdf(self, arxiv_id: str, use_export_url: bool = True) -> bytes:
            return f"pdf-for-{arxiv_id}".encode()

    heavy_mocks["arxiv"].ArxivClient = _FakeArxivClient

    from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher
    ref = ArxivRef(arxiv_id="2301.00001")
    result = ArxivFetcher().fetch(ref)

    assert result.content == b"pdf-for-2301.00001"
    assert result.content_type is ContentType.PDF


# ---------------------------------------------------------------------------
# InlineContentFetcher
# ---------------------------------------------------------------------------

def test_inline_content_fetcher_returns_embedded_bytes():
    body = b"<html><body>hello</body></html>"
    ref = InlineHtmlRef(body=body)
    result = InlineContentFetcher().fetch(ref)
    assert isinstance(result, ConversionInput)
    assert result.content == body
    assert result.content_type is ContentType.HTML


def test_inline_content_fetcher_content_type_is_html():
    ref = InlineHtmlRef(body=b"<p>text</p>")
    assert InlineContentFetcher().fetch(ref).content_type is ContentType.HTML


# ---------------------------------------------------------------------------
# SingleFileFetcher — skeleton
# ---------------------------------------------------------------------------

def test_singlefile_fetcher_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        SingleFileFetcher().fetch(SingleFileRef(path="/some/file.html"))


