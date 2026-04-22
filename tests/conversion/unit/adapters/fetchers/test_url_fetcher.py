"""Unit tests for UrlFetcher."""

from __future__ import annotations

import asyncio

import pytest

from aizk.conversion.adapters.fetchers.url import UrlFetcher
from aizk.conversion.core.errors import FetchError
from aizk.conversion.core.source_ref import UrlRef
from aizk.conversion.core.types import ContentType
from aizk.conversion.utilities.config import ConversionConfig, KarakeepFetcherConfig


def test_karakeep_asset_url_uses_content_type_hint(monkeypatch):
    async def _fake_fetch(_asset_id: str) -> bytes:
        return b"%PDF-1.7"

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.url.fetch_karakeep_asset",
        _fake_fetch,
    )

    fetcher = UrlFetcher(
        ConversionConfig(),
        KarakeepFetcherConfig(base_url="https://karakeep.example.com", api_key=""),
    )

    result = fetcher.fetch(
        UrlRef(
            url="https://karakeep.example.com/api/v1/assets/asset-123",
            content_type_hint=ContentType.PDF,
        )
    )

    assert result.content == b"%PDF-1.7"
    assert result.content_type is ContentType.PDF


@pytest.mark.parametrize(
    ("hint", "expected_content_type"),
    [
        (None, ContentType.HTML),
        (ContentType.PDF, ContentType.PDF),
    ],
)
def test_karakeep_asset_url_prefers_hint_over_path_suffix(monkeypatch, hint, expected_content_type):
    async def _fake_fetch(_asset_id: str) -> bytes:
        return b"payload"

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.url.fetch_karakeep_asset",
        _fake_fetch,
    )

    fetcher = UrlFetcher(
        ConversionConfig(),
        KarakeepFetcherConfig(base_url="https://karakeep.example.com", api_key=""),
    )

    result = fetcher.fetch(
        UrlRef(
            url="https://karakeep.example.com/api/v1/assets/asset-456",
            content_type_hint=hint,
        )
    )

    assert result.content_type is expected_content_type


def test_fetch_http_rejects_declared_content_length_over_cap(monkeypatch):
    class _Response:
        headers = {"content-type": "application/pdf", "content-length": "6"}

        async def aiter_bytes(self):
            yield b"123456"

        def raise_for_status(self) -> None:
            return

    class _StreamContext:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _AsyncClient:
        def __init__(self, **kwargs):
            return

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method: str, url: str):
            assert method == "GET"
            return _StreamContext()

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.url.httpx.AsyncClient", _AsyncClient)

    fetcher = UrlFetcher(
        ConversionConfig(_env_file=None, fetch_max_response_bytes=5),
        KarakeepFetcherConfig(_env_file=None),
    )

    with pytest.raises(FetchError, match="exceeds configured limit"):
        asyncio.run(fetcher._fetch_http("https://example.com/oversized.pdf"))


def test_fetch_http_rejects_streamed_body_over_cap(monkeypatch):
    class _Response:
        headers = {"content-type": "text/html"}

        async def aiter_bytes(self):
            yield b"1234"
            yield b"56"

        def raise_for_status(self) -> None:
            return

    class _StreamContext:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _AsyncClient:
        def __init__(self, **kwargs):
            return

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method: str, url: str):
            assert method == "GET"
            return _StreamContext()

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.url.httpx.AsyncClient", _AsyncClient)

    fetcher = UrlFetcher(
        ConversionConfig(_env_file=None, fetch_max_response_bytes=5),
        KarakeepFetcherConfig(_env_file=None),
    )

    with pytest.raises(FetchError, match="exceeds configured limit"):
        asyncio.run(fetcher._fetch_http("https://example.com/oversized-stream"))
