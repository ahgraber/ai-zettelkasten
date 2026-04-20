"""Unit tests for conversion worker content fetchers.

Covers:
* Error classification (retryable vs permanent) per spec §92-113.
* arXiv bookmark resolution paths per spec §114-135.
* GitHub README preference ordering (branch + format) per spec §136-151.
"""

from __future__ import annotations

import httpx
from pyleak import no_task_leaks
import pytest

from aizk.conversion.utilities import fetch_helpers as _fetch_helpers
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import fetcher


class _DummyBookmark:
    def __init__(self, bookmark_id: str = "bookmark-1"):
        self.id = bookmark_id


# ---------------------------------------------------------------------------
# Error classification — retryable vs permanent
# ---------------------------------------------------------------------------


class TestFetchErrorClassification:
    def test_base_fetch_error_is_retryable(self):
        exc = fetcher.FetchError("transient")
        assert exc.retryable is True

    def test_bookmark_content_unavailable_is_permanent(self):
        exc = fetcher.BookmarkContentUnavailableError("no content")
        assert exc.retryable is False

    def test_github_readme_not_found_is_permanent(self):
        exc = fetcher.GitHubReadmeNotFoundError("no readme")
        assert exc.retryable is False

    def test_arxiv_pdf_fetch_error_is_retryable(self):
        exc = fetcher.ArxivPdfFetchError("transient arxiv")
        assert exc.retryable is True


# ---------------------------------------------------------------------------
# fetch_karakeep_asset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_fetch_karakeep_asset_returns_bytes(monkeypatch):
    class _DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

        async def get_asset(self, asset_id: str) -> bytes:
            return f"bytes-{asset_id}".encode()

    monkeypatch.setattr(_fetch_helpers, "KarakeepClient", lambda: _DummyClient())

    async with no_task_leaks(action="raise"):
        result = await fetcher.fetch_karakeep_asset("asset-1")

    assert result == b"bytes-asset-1"


@pytest.mark.asyncio(loop_scope="function")
async def test_fetch_karakeep_asset_wraps_errors_as_retryable():
    """KaraKeep transport failures surface as retryable FetchError per spec §102-106."""

    class _FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

        async def get_asset(self, asset_id: str) -> bytes:
            raise httpx.ConnectError("connection refused")

    import unittest.mock as _mock

    with _mock.patch.object(_fetch_helpers, "KarakeepClient", lambda: _FailingClient()):
        with pytest.raises(fetcher.FetchError) as excinfo:
            await fetcher.fetch_karakeep_asset("asset-X")

    assert excinfo.value.retryable is True
    assert "asset-X" in str(excinfo.value)


# ---------------------------------------------------------------------------
# fetch_arxiv
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_fetch_arxiv_abstract_url_downloads_pdf(monkeypatch):
    """arXiv abstract-page bookmarks resolve paper ID and download PDF (spec §118-122)."""
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bm: "https://arxiv.org/abs/1706.03762")
    monkeypatch.setattr(fetcher, "is_arxiv_url", lambda _u: True)
    monkeypatch.setattr(fetcher, "get_arxiv_id", lambda _u: "1706.03762")

    called = {}

    async def _fake_pdf_fetch(arxiv_id: str, _config):
        called["arxiv_id"] = arxiv_id
        return b"%PDF-1.4"

    monkeypatch.setattr(fetcher, "fetch_arxiv_pdf", _fake_pdf_fetch)

    result = await fetcher.fetch_arxiv(bookmark, config)

    assert result == b"%PDF-1.4"
    assert called["arxiv_id"] == "1706.03762"


@pytest.mark.asyncio(loop_scope="function")
async def test_fetch_arxiv_pdf_asset_uses_provided_bytes(monkeypatch):
    """Pre-fetched asset bytes are used directly when the bookmark is a PDF asset (spec §124-128)."""
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    # Non-abstract URL triggers asset branch
    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bm: "https://arxiv.org/pdf/1706.03762")
    monkeypatch.setattr(fetcher, "is_arxiv_url", lambda _u: True)
    monkeypatch.setattr(fetcher, "get_arxiv_id", lambda _u: "1706.03762")
    monkeypatch.setattr(fetcher, "is_pdf_asset", lambda _bm: True)

    # Asserts PDF fetch is NOT called — asset bytes short-circuit.
    async def _should_not_be_called(*_a, **_kw):
        raise AssertionError("fetch_arxiv_pdf must not be called when asset_bytes is provided")

    monkeypatch.setattr(fetcher, "fetch_arxiv_pdf", _should_not_be_called)

    result = await fetcher.fetch_arxiv(bookmark, config, asset_bytes=b"pre-fetched-pdf")

    assert result == b"pre-fetched-pdf"


@pytest.mark.asyncio(loop_scope="function")
async def test_fetch_arxiv_pdf_asset_fetches_from_karakeep(monkeypatch):
    """Without pre-fetched bytes, the PDF asset is fetched via KaraKeep (spec §124-128)."""
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bm: "https://arxiv.org/pdf/1706.03762")
    monkeypatch.setattr(fetcher, "is_arxiv_url", lambda _u: True)
    monkeypatch.setattr(fetcher, "get_arxiv_id", lambda _u: "1706.03762")
    monkeypatch.setattr(fetcher, "is_pdf_asset", lambda _bm: True)
    monkeypatch.setattr(fetcher, "get_bookmark_asset_id", lambda _bm: "asset-7")

    captured = {}

    async def _fake_fetch_asset(asset_id: str):
        captured["asset_id"] = asset_id
        return b"karakeep-pdf"

    monkeypatch.setattr(fetcher, "fetch_karakeep_asset", _fake_fetch_asset)

    result = await fetcher.fetch_arxiv(bookmark, config)

    assert result == b"karakeep-pdf"
    assert captured["asset_id"] == "asset-7"


@pytest.mark.asyncio(loop_scope="function")
async def test_fetch_arxiv_rejects_non_arxiv_bookmark_permanently(monkeypatch):
    """Non-arXiv bookmarks raise the permanent BookmarkContentUnavailableError."""
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bm: "https://example.com/paper")
    monkeypatch.setattr(fetcher, "is_arxiv_url", lambda _u: False)

    with pytest.raises(fetcher.BookmarkContentUnavailableError) as excinfo:
        await fetcher.fetch_arxiv(bookmark, config)

    assert excinfo.value.retryable is False


# ---------------------------------------------------------------------------
# fetch_github_readme — preference ordering + permanent failure
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Async httpx client stub that records .get() URLs and replies from a table."""

    def __init__(self, responses_by_url: dict[str, int], body: bytes = b"# README") -> None:
        self._responses_by_url = responses_by_url
        self._body = body
        self.requested_urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url: str) -> httpx.Response:
        self.requested_urls.append(url)
        status = self._responses_by_url.get(url, 404)
        return httpx.Response(status_code=status, content=self._body if status == 200 else b"")


@pytest.mark.asyncio(loop_scope="function")
async def test_github_readme_prefers_md_over_rst_on_main_branch(monkeypatch):
    """README.md on main wins over README.rst on master (spec §136-138)."""
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bm: "https://github.com/owner/repo/path")
    monkeypatch.setattr(fetcher, "is_github_url", lambda _u: True)
    monkeypatch.setattr(fetcher, "is_github_pages_url", lambda _u: False)
    monkeypatch.setattr(fetcher, "is_github_repo_root", lambda _u: False)
    monkeypatch.setattr(fetcher, "source_mentions_readme", lambda _u: False)
    monkeypatch.setattr(fetcher, "parse_github_owner_repo", lambda _u: ("owner", "repo"))

    url_md_main = "https://raw.githubusercontent.com/owner/repo/main/README.md"
    url_rst_master = "https://raw.githubusercontent.com/owner/repo/master/README.rst"
    stub = _RecordingClient({url_md_main: 200, url_rst_master: 200}, body=b"# md content")
    monkeypatch.setattr(fetcher.httpx, "AsyncClient", lambda **_kw: stub)

    result = await fetcher.fetch_github_readme(bookmark, config)

    assert result == b"# md content"
    # First reachable match wins; master branch is never reached.
    assert url_md_main in stub.requested_urls
    assert url_rst_master not in stub.requested_urls


@pytest.mark.asyncio(loop_scope="function")
async def test_github_readme_falls_back_to_rst_when_md_missing(monkeypatch):
    """Format preference: .md > .MD > .md-lower > .rst > .txt > README (spec §138)."""
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bm: "https://github.com/owner/repo")
    monkeypatch.setattr(fetcher, "is_github_url", lambda _u: True)
    monkeypatch.setattr(fetcher, "is_github_pages_url", lambda _u: False)
    monkeypatch.setattr(fetcher, "is_github_repo_root", lambda _u: False)
    monkeypatch.setattr(fetcher, "source_mentions_readme", lambda _u: False)
    monkeypatch.setattr(fetcher, "parse_github_owner_repo", lambda _u: ("owner", "repo"))

    # Only the .rst variant on main is reachable.
    url_rst = "https://raw.githubusercontent.com/owner/repo/main/README.rst"
    stub = _RecordingClient({url_rst: 200}, body=b"rst body")
    monkeypatch.setattr(fetcher.httpx, "AsyncClient", lambda **_kw: stub)

    result = await fetcher.fetch_github_readme(bookmark, config)

    assert result == b"rst body"
    # All three .md variants were tried before .rst.
    md_attempts = [u for u in stub.requested_urls if u.endswith((".md", ".MD"))]
    assert len(md_attempts) >= 3


@pytest.mark.asyncio(loop_scope="function")
async def test_github_readme_no_readme_raises_permanent_error(monkeypatch):
    """Repo with no README on any branch → permanent GitHubReadmeNotFoundError (spec §146-150)."""
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bm: "https://github.com/owner/repo")
    monkeypatch.setattr(fetcher, "is_github_url", lambda _u: True)
    monkeypatch.setattr(fetcher, "is_github_pages_url", lambda _u: False)
    monkeypatch.setattr(fetcher, "is_github_repo_root", lambda _u: False)
    monkeypatch.setattr(fetcher, "source_mentions_readme", lambda _u: False)
    monkeypatch.setattr(fetcher, "parse_github_owner_repo", lambda _u: ("owner", "repo"))

    stub = _RecordingClient({})  # everything 404s
    monkeypatch.setattr(fetcher.httpx, "AsyncClient", lambda **_kw: stub)

    with pytest.raises(fetcher.GitHubReadmeNotFoundError) as excinfo:
        await fetcher.fetch_github_readme(bookmark, config)

    assert excinfo.value.retryable is False
    # Must have attempted both branches.
    assert any("/main/" in u for u in stub.requested_urls)
    assert any("/master/" in u for u in stub.requested_urls)


@pytest.mark.asyncio(loop_scope="function")
async def test_github_readme_rejects_non_github_bookmark(monkeypatch):
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bm: "https://example.com/x")
    monkeypatch.setattr(fetcher, "is_github_url", lambda _u: False)

    with pytest.raises(fetcher.BookmarkContentUnavailableError):
        await fetcher.fetch_github_readme(bookmark, config)


@pytest.mark.asyncio(loop_scope="function")
async def test_github_pages_uses_bookmark_html(monkeypatch):
    """GitHub Pages bookmarks return bundled HTML content rather than fetching (spec §140-144)."""
    bookmark = _DummyBookmark()
    config = ConversionConfig(_env_file=None, fetch_timeout_seconds=5)

    monkeypatch.setattr(fetcher, "get_bookmark_source_url", lambda _bm: "https://user.github.io/site")
    monkeypatch.setattr(fetcher, "is_github_url", lambda _u: True)
    monkeypatch.setattr(fetcher, "is_github_pages_url", lambda _u: True)

    async with no_task_leaks(action="raise"):
        result = await fetcher.fetch_github_readme(
            bookmark,
            config,
            html_content="<html>pages</html>",
        )

    assert result == b"<html>pages</html>"
