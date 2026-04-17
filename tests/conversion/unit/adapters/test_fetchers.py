"""Tests for fetcher adapters: KarakeepBookmarkResolver, ArxivFetcher, InlineContentFetcher.

All tests that exercise resolve() / fetch() inject mock stubs for the heavy
utility and worker modules that are unavailable in the unit-test venv
(karakeep_client, defusedxml, httpx, validators, etc.).
"""

from __future__ import annotations

import importlib
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

    result = KarakeepBookmarkResolver().resolve(KarakeepBookmarkRef(bookmark_id="bk-1"))

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
    monkeypatch.setenv("KARAKEEP_BASE_URL", "https://karakeep.example.com")

    result = KarakeepBookmarkResolver().resolve(KarakeepBookmarkRef(bookmark_id="bk-1"))

    assert isinstance(result, ArxivRef)
    assert result.karakeep_asset_url == "https://karakeep.example.com/api/v1/assets/asset-xyz"


def test_github_bookmark_returns_github_readme_ref(heavy_mocks):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://github.com/owner/repo"
    mocks["bm"].detect_source_type.return_value = "github"
    mocks["gh"].is_github_pages_url.return_value = False
    mocks["gh"].parse_github_owner_repo.return_value = ("owner", "repo")

    result = KarakeepBookmarkResolver().resolve(KarakeepBookmarkRef(bookmark_id="bk-2"))

    assert isinstance(result, GithubReadmeRef)
    assert result.owner == "owner"
    assert result.repo == "repo"


def test_github_pages_bookmark_returns_url_ref(heavy_mocks):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://owner.github.io/site"
    mocks["bm"].detect_source_type.return_value = "github"
    mocks["gh"].is_github_pages_url.return_value = True

    result = KarakeepBookmarkResolver().resolve(KarakeepBookmarkRef(bookmark_id="bk-3"))

    assert isinstance(result, UrlRef)
    assert result.url == "https://owner.github.io/site"


def test_pdf_asset_bookmark_returns_url_ref(heavy_mocks, monkeypatch):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://example.com/doc"
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].is_pdf_asset.return_value = True
    mocks["bm"].get_bookmark_asset_id.return_value = "pdf-asset-1"
    monkeypatch.setenv("KARAKEEP_BASE_URL", "https://karakeep.example.com")

    result = KarakeepBookmarkResolver().resolve(KarakeepBookmarkRef(bookmark_id="bk-4"))

    assert isinstance(result, UrlRef)
    assert "pdf-asset-1" in result.url


def test_precrawled_archive_returns_url_ref(heavy_mocks, monkeypatch):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://example.com/page"
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].is_pdf_asset.return_value = False
    mocks["bm"].is_precrawled_archive_asset.return_value = True
    mocks["bm"].get_bookmark_asset_id.return_value = "archive-1"
    monkeypatch.setenv("KARAKEEP_BASE_URL", "https://karakeep.example.com")

    result = KarakeepBookmarkResolver().resolve(KarakeepBookmarkRef(bookmark_id="bk-5"))

    assert isinstance(result, UrlRef)
    assert "archive-1" in result.url


def test_html_content_bookmark_returns_url_ref(heavy_mocks):
    mocks = heavy_mocks
    mocks["bm"].get_bookmark_source_url.return_value = "https://example.com/article"
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].get_bookmark_html_content.return_value = "<html><body>content</body></html>"

    result = KarakeepBookmarkResolver().resolve(KarakeepBookmarkRef(bookmark_id="bk-6"))

    assert isinstance(result, UrlRef)
    assert result.url == "https://example.com/article"


def test_text_only_bookmark_returns_inline_html_ref(heavy_mocks):
    mocks = heavy_mocks
    # No source_url — raises BookmarkContentError so source_url becomes None
    mocks["bm"].get_bookmark_source_url.side_effect = _BookmarkContentError("no url")
    mocks["bm"].detect_source_type.return_value = "other"
    mocks["bm"].get_bookmark_html_content.return_value = None
    mocks["bm"].get_bookmark_text_content.return_value = "some plain text"

    result = KarakeepBookmarkResolver().resolve(KarakeepBookmarkRef(bookmark_id="bk-7"))

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
        KarakeepBookmarkResolver().resolve(KarakeepBookmarkRef(bookmark_id="bk-8"))


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


# ---------------------------------------------------------------------------
# Import path compatibility — sys.modules surgery required because workers.fetcher
# has top-level imports of karakeep_client / httpx which are absent from this venv.
# ---------------------------------------------------------------------------

_HEAVY_FOR_WORKERS_FETCHER = [
    "httpx",
    "karakeep_client", "karakeep_client.karakeep", "karakeep_client.models",
    "aizk.conversion.utilities.arxiv_utils",
    "aizk.conversion.utilities.bookmark_utils",
    "aizk.conversion.utilities.github_utils",
    "aizk.utilities.url_utils",
    "aizk.utilities.limiters",
    "defusedxml", "defusedxml.ElementTree",
    "requests",
    "validators",
]


def _make_bookmark_utils_mock() -> MagicMock:
    """Mock for aizk.conversion.utilities.bookmark_utils with real exception classes.

    workers/fetcher.py defines ``BookmarkContentUnavailableError(FetchError, BookmarkContentError)``
    at module load time — so BookmarkContentError must be a real exception class, not a MagicMock.
    """
    mock = MagicMock(name="bookmark_utils")
    mock.BookmarkContentError = _BookmarkContentError
    mock.BookmarkContentUnavailableError = _BookmarkContentUnavailableError
    return mock


def _import_workers_fetcher_with_mocks():
    """Import workers.fetcher with heavy deps mocked; return the module."""
    prev = sys.modules.pop("aizk.conversion.workers.fetcher", None)
    mocks: dict[str, object] = {}
    try:
        for mod_name in _HEAVY_FOR_WORKERS_FETCHER:
            if mod_name not in sys.modules:
                if mod_name == "aizk.conversion.utilities.bookmark_utils":
                    stub = _make_bookmark_utils_mock()
                else:
                    stub = MagicMock()
                mocks[mod_name] = stub
                sys.modules[mod_name] = stub
        return importlib.import_module("aizk.conversion.workers.fetcher"), prev, mocks
    except Exception:
        # Restore state on failure
        for m in mocks:
            sys.modules.pop(m, None)
        sys.modules.pop("aizk.conversion.workers.fetcher", None)
        if prev is not None:
            sys.modules["aizk.conversion.workers.fetcher"] = prev
        raise


def _cleanup_workers_fetcher_mocks(prev, mocks):
    for m in mocks:
        sys.modules.pop(m, None)
    sys.modules.pop("aizk.conversion.workers.fetcher", None)
    if prev is not None:
        sys.modules["aizk.conversion.workers.fetcher"] = prev


def test_karakeep_resolver_importable_from_workers_fetcher():
    """KarakeepBookmarkResolver re-exported from workers.fetcher resolves to adapter class."""
    module, prev, mocks = _import_workers_fetcher_with_mocks()
    try:
        ReExported = module.KarakeepBookmarkResolver
        assert ReExported.__name__ == "KarakeepBookmarkResolver"
        assert ReExported.__module__ == "aizk.conversion.adapters.fetchers.karakeep"
    finally:
        _cleanup_workers_fetcher_mocks(prev, mocks)


def test_arxiv_fetcher_importable_from_workers_fetcher():
    module, prev, mocks = _import_workers_fetcher_with_mocks()
    try:
        ReExported = module.ArxivFetcher
        assert ReExported.__name__ == "ArxivFetcher"
        assert ReExported.__module__ == "aizk.conversion.adapters.fetchers.arxiv"
    finally:
        _cleanup_workers_fetcher_mocks(prev, mocks)


def test_github_readme_fetcher_importable_from_workers_fetcher():
    module, prev, mocks = _import_workers_fetcher_with_mocks()
    try:
        ReExported = module.GithubReadmeFetcher
        assert ReExported.__name__ == "GithubReadmeFetcher"
        assert ReExported.__module__ == "aizk.conversion.adapters.fetchers.github"
    finally:
        _cleanup_workers_fetcher_mocks(prev, mocks)


def test_inline_content_fetcher_importable_from_workers_fetcher():
    module, prev, mocks = _import_workers_fetcher_with_mocks()
    try:
        ReExported = module.InlineContentFetcher
        assert ReExported.__name__ == "InlineContentFetcher"
        assert ReExported.__module__ == "aizk.conversion.adapters.fetchers.inline"
    finally:
        _cleanup_workers_fetcher_mocks(prev, mocks)


def _import_utility_module_with_mocks(mod_name: str, heavy: list[str]):
    """Import a utility module with heavy deps pre-mocked; return module + cleanup state."""
    prev = sys.modules.pop(mod_name, None)
    mocks: dict[str, object] = {}
    for dep in heavy:
        if dep not in sys.modules:
            mocks[dep] = sys.modules.setdefault(dep, MagicMock())
    module = importlib.import_module(mod_name)
    return module, prev, mocks


def _cleanup_utility_mocks(mod_name: str, prev, mocks):
    for m in mocks:
        sys.modules.pop(m, None)
    sys.modules.pop(mod_name, None)
    if prev is not None:
        sys.modules[mod_name] = prev


_BOOKMARK_UTILS_HEAVY = [
    "karakeep_client", "karakeep_client.karakeep", "karakeep_client.models",
    "aizk.conversion.utilities.arxiv_utils",
    "aizk.utilities.url_utils", "aizk.utilities.limiters",
    "defusedxml", "defusedxml.ElementTree",
    "requests", "httpx", "validators",
]

_ARXIV_UTILS_HEAVY = [
    "defusedxml", "defusedxml.ElementTree",
    "requests", "httpx",
    "aizk.utilities.url_utils", "aizk.utilities.limiters",
    "validators",
]

_GITHUB_UTILS_HEAVY = [
    "aizk.utilities.url_utils", "validators",
]


def test_karakeep_resolver_importable_from_bookmark_utils():
    """KarakeepBookmarkResolver re-exported from bookmark_utils resolves to adapter class."""
    mod_name = "aizk.conversion.utilities.bookmark_utils"
    module, prev, mocks = _import_utility_module_with_mocks(mod_name, _BOOKMARK_UTILS_HEAVY)
    try:
        ReExported = module.KarakeepBookmarkResolver
        assert ReExported.__name__ == "KarakeepBookmarkResolver"
        assert ReExported.__module__ == "aizk.conversion.adapters.fetchers.karakeep"
    finally:
        _cleanup_utility_mocks(mod_name, prev, mocks)


def test_arxiv_fetcher_importable_from_arxiv_utils():
    """ArxivFetcher re-exported from arxiv_utils resolves to adapter class."""
    mod_name = "aizk.conversion.utilities.arxiv_utils"
    module, prev, mocks = _import_utility_module_with_mocks(mod_name, _ARXIV_UTILS_HEAVY)
    try:
        ReExported = module.ArxivFetcher
        assert ReExported.__name__ == "ArxivFetcher"
        assert ReExported.__module__ == "aizk.conversion.adapters.fetchers.arxiv"
    finally:
        _cleanup_utility_mocks(mod_name, prev, mocks)


def test_github_readme_fetcher_importable_from_github_utils():
    """GithubReadmeFetcher re-exported from github_utils resolves to adapter class."""
    mod_name = "aizk.conversion.utilities.github_utils"
    module, prev, mocks = _import_utility_module_with_mocks(mod_name, _GITHUB_UTILS_HEAVY)
    try:
        ReExported = module.GithubReadmeFetcher
        assert ReExported.__name__ == "GithubReadmeFetcher"
        assert ReExported.__module__ == "aizk.conversion.adapters.fetchers.github"
    finally:
        _cleanup_utility_mocks(mod_name, prev, mocks)
