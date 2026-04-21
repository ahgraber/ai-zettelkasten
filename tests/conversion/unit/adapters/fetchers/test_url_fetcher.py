"""Unit tests for UrlFetcher."""

from __future__ import annotations

import pytest

from aizk.conversion.adapters.fetchers.url import UrlFetcher
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
