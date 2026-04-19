"""Unit tests for ArxivFetcher adapter.

All tests are hermetic: no .env, no real network calls.
"""

from __future__ import annotations

import pytest

from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher
from aizk.conversion.core.source_ref import ArxivRef
from aizk.conversion.core.types import ContentType, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig


def _config() -> ConversionConfig:
    return ConversionConfig(_env_file=None, fetch_timeout_seconds=30)


# ---------------------------------------------------------------------------
# Class-level structural tests
# ---------------------------------------------------------------------------


def test_arxiv_fetcher_produces_pdf_only():
    assert ArxivFetcher.produces == frozenset({ContentType.PDF})


def test_arxiv_fetcher_satisfies_content_fetcher_structurally():
    fetcher = ArxivFetcher(_config())
    assert hasattr(fetcher, "fetch")
    assert hasattr(ArxivFetcher, "produces")
    assert callable(fetcher.fetch)
    from aizk.conversion.core.protocols import ContentFetcher

    assert isinstance(fetcher, ContentFetcher)


# ---------------------------------------------------------------------------
# Step 1 — KaraKeep asset URL
# ---------------------------------------------------------------------------


def test_arxiv_fetcher_uses_karakeep_asset_when_arxiv_pdf_url_is_karakeep_url(monkeypatch):
    monkeypatch.setenv("KARAKEEP_BASE_URL", "https://karakeep.example.com")

    pdf_bytes = b"%PDF-1.4 karakeep"
    calls = {"fetch_arxiv_pdf": 0}

    async def _fake_karakeep_asset(asset_id: str) -> bytes:
        assert asset_id == "asset-abc"
        return pdf_bytes

    async def _fake_fetch_arxiv_pdf(arxiv_id, config):
        calls["fetch_arxiv_pdf"] += 1
        return b"should not be called"

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_karakeep_asset",
        _fake_karakeep_asset,
    )
    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf",
        _fake_fetch_arxiv_pdf,
    )

    fetcher = ArxivFetcher(_config())
    ref = ArxivRef(
        arxiv_id="2301.12345",
        arxiv_pdf_url="https://karakeep.example.com/api/v1/assets/asset-abc",
    )
    result = fetcher.fetch(ref)

    assert isinstance(result, ConversionInput)
    assert result.content == pdf_bytes
    assert result.content_type == ContentType.PDF
    assert calls["fetch_arxiv_pdf"] == 0


# ---------------------------------------------------------------------------
# Step 2 — non-KaraKeep arxiv_pdf_url (direct HTTP)
# ---------------------------------------------------------------------------


def test_arxiv_fetcher_uses_arxiv_pdf_url_when_non_karakeep(monkeypatch):
    pdf_bytes = b"%PDF-1.4 arxiv-pdf-url"

    import httpx

    class _FakeResponse:
        content = pdf_bytes
        status_code = 200

        def raise_for_status(self):
            pass

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.arxiv.httpx.AsyncClient", _FakeAsyncClient)

    fetcher = ArxivFetcher(_config())
    ref = ArxivRef(
        arxiv_id="2301.12345",
        arxiv_pdf_url="https://arxiv.org/pdf/2301.12345",
    )
    result = fetcher.fetch(ref)

    assert result.content == pdf_bytes
    assert result.content_type == ContentType.PDF


# ---------------------------------------------------------------------------
# Step 3 — no arxiv_pdf_url → arXiv API resolution
# ---------------------------------------------------------------------------


def test_arxiv_fetcher_constructs_url_from_arxiv_id_when_no_pdf_url(monkeypatch):
    pdf_bytes = b"%PDF-1.4 abstract-page"

    async def _fake_fetch_arxiv_pdf(arxiv_id: str, config):
        assert arxiv_id == "2301.12345"
        return pdf_bytes

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.arxiv.fetch_arxiv_pdf",
        _fake_fetch_arxiv_pdf,
    )

    fetcher = ArxivFetcher(_config())
    ref = ArxivRef(arxiv_id="2301.12345", arxiv_pdf_url=None)
    result = fetcher.fetch(ref)

    assert result.content == pdf_bytes
    assert result.content_type == ContentType.PDF
