"""Unit tests for UrlFetcher."""

from __future__ import annotations

import asyncio

import pytest

from aizk.conversion.adapters.fetchers.url import UrlFetcher
from aizk.conversion.core.errors import FetchError
from aizk.conversion.core.source_ref import UrlRef
from aizk.conversion.core.types import ContentType, SourceMetadata
from aizk.conversion.utilities.config import ConversionConfig, KarakeepFetcherConfig

_KARAKEEP_CFG = KarakeepFetcherConfig(base_url="https://karakeep.example.com", api_key="")


def _make_fetcher(karakeep_cfg=None):
    return UrlFetcher(ConversionConfig(_env_file=None), karakeep_cfg or KarakeepFetcherConfig(_env_file=None))


def test_karakeep_asset_url_uses_content_type_hint(monkeypatch):
    async def _fake_fetch(_asset_id: str) -> bytes:
        return b"%PDF-1.7"

    monkeypatch.setattr(
        "aizk.conversion.adapters.fetchers.url.fetch_karakeep_asset",
        _fake_fetch,
    )

    fetcher = UrlFetcher(ConversionConfig(), _KARAKEEP_CFG)

    result = fetcher.fetch(
        UrlRef(
            url="https://karakeep.example.com/api/v1/assets/asset-123",
            content_type_hint=ContentType.PDF,
        ),
        SourceMetadata(),
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

    fetcher = UrlFetcher(ConversionConfig(), _KARAKEEP_CFG)

    result = fetcher.fetch(
        UrlRef(
            url="https://karakeep.example.com/api/v1/assets/asset-456",
            content_type_hint=hint,
        ),
        SourceMetadata(),
    )

    assert result.content_type is expected_content_type


def test_url_fetcher_direct_path_populates_source_meta(monkeypatch):
    """Direct fetch (no resolver hop) populates source_url, document_base_url, normalized_url."""

    async def _fake(url, **kwargs):
        return b"<html/>", {"content-type": "text/html"}

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.url.egress_fetch_bytes", _fake)

    fetcher = _make_fetcher()
    result = fetcher.fetch(UrlRef(url="https://example.com/page"), SourceMetadata())

    assert result.source_meta.source_url is not None
    assert "example.com" in result.source_meta.source_url
    assert result.source_meta.document_base_url is not None


def test_url_fetcher_normalize_url_failure_yields_none_and_logs_debug(monkeypatch, caplog):
    """When normalize_url raises, normalized_url is None, the job continues, and a debug log is emitted."""
    import logging

    async def _fake(url, **kwargs):
        return b"<html/>", {"content-type": "text/html"}

    def _raising_normalize(url):
        raise ValueError(f"malformed url: {url}")

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.url.egress_fetch_bytes", _fake)
    monkeypatch.setattr("aizk.utilities.url_utils.normalize_url", _raising_normalize)

    fetcher = _make_fetcher()
    with caplog.at_level(logging.DEBUG, logger="aizk.conversion.adapters.fetchers.url"):
        result = fetcher.fetch(UrlRef(url="https://example.com/page"), SourceMetadata())

    assert result.source_meta.normalized_url is None
    # Job continues: source_url and content are still populated.
    assert result.source_meta.source_url == "https://example.com/page"
    assert result.content == b"<html/>"
    assert any("normalize_url failed" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG), (
        "expected a DEBUG log line announcing normalize_url failure"
    )


def test_url_fetcher_resolved_path_preserves_resolver_source_url(monkeypatch):
    """When resolver already set source_url, fetcher must not overwrite it with the asset URL."""

    async def _fake_fetch(_asset_id: str) -> bytes:
        return b"<html/>"

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.url.fetch_karakeep_asset", _fake_fetch)

    fetcher = UrlFetcher(ConversionConfig(), _KARAKEEP_CFG)
    resolver_meta = SourceMetadata(source_url="https://original-page.com/post", resolver_title="My Post")

    result = fetcher.fetch(
        UrlRef(url="https://karakeep.example.com/api/v1/assets/asset-xyz"),
        resolver_meta,
    )

    # Resolver-supplied source_url wins over the asset URL
    assert result.source_meta.source_url == "https://original-page.com/post"
    assert result.source_meta.resolver_title == "My Post"


def test_url_fetcher_does_not_backfill_normalized_url_when_resolver_supplied_source_url(monkeypatch):
    """When resolver supplied source_url but left normalized_url None, fetcher MUST NOT
    backfill normalized_url from the asset URL.

    Conversion-worker spec invariant:
    ``normalized_url == normalize_url(source_url)``.
    Backfilling from a different URL (the asset fetch URL) would publish a normalized
    form that does not correspond to source_url — breaking dedup queries and leaking
    asset URLs into Source.normalized_url / manifest.normalized_url.
    """

    async def _fake_fetch(_asset_id: str) -> bytes:
        return b"<html/>"

    monkeypatch.setattr("aizk.conversion.adapters.fetchers.url.fetch_karakeep_asset", _fake_fetch)

    fetcher = UrlFetcher(ConversionConfig(), _KARAKEEP_CFG)
    resolver_meta = SourceMetadata(
        source_url="https://original-page.com/post",
        # normalized_url intentionally left None (e.g. resolver's normalize_url raised)
    )

    result = fetcher.fetch(
        UrlRef(url="https://karakeep.example.com/api/v1/assets/asset-xyz"),
        resolver_meta,
    )

    assert result.source_meta.source_url == "https://original-page.com/post"
    # normalized_url stays None — fetcher does not derive it from a different URL
    assert result.source_meta.normalized_url is None
    # document_base_url also not backfilled from asset URL when resolver owns source_url
    assert result.source_meta.document_base_url is None


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

    fetcher = _make_fetcher()
    with pytest.raises(EgressPolicyError):
        fetcher.fetch(UrlRef(url="https://example.com/anything"), SourceMetadata())
