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


def test_fetch_http_returns_pdf_content_type_for_pdf_response(monkeypatch):
    """Generic HTTP path delegates to ``egress_fetch_bytes`` and detects PDF from content-type."""

    async def _fake(url, **kwargs):
        return b"%PDF-1.4 ok", {"content-type": "application/pdf"}

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.url.egress_fetch_bytes",
        _fake,
    )

    fetcher = UrlFetcher(
        ConversionConfig(_env_file=None),
        KarakeepFetcherConfig(_env_file=None),
    )
    body, ct = asyncio.run(fetcher._fetch_http("https://example.com/x.pdf"))
    assert body == b"%PDF-1.4 ok"
    assert ct is ContentType.PDF


def test_fetch_http_returns_html_content_type_when_not_pdf(monkeypatch):
    """Non-pdf content-type defaults to HTML."""

    async def _fake(url, **kwargs):
        return b"<html/>", {"content-type": "text/html; charset=utf-8"}

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.url.egress_fetch_bytes",
        _fake,
    )

    fetcher = UrlFetcher(
        ConversionConfig(_env_file=None),
        KarakeepFetcherConfig(_env_file=None),
    )
    body, ct = asyncio.run(fetcher._fetch_http("https://example.com/x.html"))
    assert body == b"<html/>"
    assert ct is ContentType.HTML


def test_fetch_http_propagates_fetch_too_large_error(monkeypatch):
    """``FetchTooLargeError`` from the helper bubbles up unchanged (non-retryable)."""
    from aizk.conversion.core.errors import FetchTooLargeError

    async def _fake(url, **kwargs):
        raise FetchTooLargeError("exceeds configured limit of 5 bytes")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.url.egress_fetch_bytes",
        _fake,
    )

    fetcher = UrlFetcher(
        ConversionConfig(_env_file=None, fetch_max_response_bytes=5),
        KarakeepFetcherConfig(_env_file=None),
    )

    with pytest.raises(FetchError, match="exceeds configured limit"):
        asyncio.run(fetcher._fetch_http("https://example.com/oversized"))


def test_fetch_propagates_egress_policy_error(monkeypatch):
    """Egress rejection of the URL surfaces as an ``EgressPolicyError`` (non-retryable)."""
    from aizk.conversion.core.errors import DenyListDestination, EgressPolicyError

    async def _fake(url, **kwargs):
        raise DenyListDestination("denied")

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.url.egress_fetch_bytes",
        _fake,
    )

    fetcher = UrlFetcher(
        ConversionConfig(_env_file=None),
        KarakeepFetcherConfig(_env_file=None),
    )
    with pytest.raises(EgressPolicyError):
        fetcher.fetch(UrlRef(url="https://example.com/anything"))
