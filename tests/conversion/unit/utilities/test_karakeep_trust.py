"""Unit tests for the KaraKeep trusted-origin asset matcher."""

from __future__ import annotations

import pytest

from aizk.conversion.utilities.fetch_helpers import is_karakeep_trusted_asset_url


@pytest.mark.parametrize(
    ("base", "candidate"),
    [
        # Exact origin + asset path
        ("https://karakeep.example.internal", "https://karakeep.example.internal/api/v1/assets/abc"),
        # Trailing slash on configured base does not block the match
        ("https://karakeep.example.internal/", "https://karakeep.example.internal/api/v1/assets/abc"),
        # Explicit default port equals implicit default port
        ("https://karakeep.example.internal:443", "https://karakeep.example.internal/api/v1/assets/abc"),
        # Hostname comparison is case-insensitive
        ("https://Karakeep.Example.Internal", "https://karakeep.example.internal/api/v1/assets/abc"),
    ],
)
def test_exact_origin_trusted_asset_matches(base, candidate):
    assert is_karakeep_trusted_asset_url(candidate, base) is True


@pytest.mark.parametrize(
    ("base", "candidate"),
    [
        # Lookalike suffix host (the historical prefix-match hole)
        (
            "https://karakeep.example.internal",
            "https://karakeep.example.internal.evil.test/api/v1/assets/abc",
        ),
        # Lookalike prefix host
        (
            "https://karakeep.example.internal",
            "https://evil-karakeep.example.internal/api/v1/assets/abc",
        ),
        # Different scheme
        (
            "https://karakeep.example.internal",
            "http://karakeep.example.internal/api/v1/assets/abc",
        ),
        # Different explicit port
        (
            "https://karakeep.example.internal",
            "https://karakeep.example.internal:8443/api/v1/assets/abc",
        ),
        # Same origin, but path is not the asset endpoint
        (
            "https://karakeep.example.internal",
            "https://karakeep.example.internal/api/v1/bookmarks/abc",
        ),
        # Path tries to ride the asset prefix but is a sibling segment
        (
            "https://karakeep.example.internal",
            "https://karakeep.example.internal/api/v1/assetshijack/abc",
        ),
        # Empty configured base
        ("", "https://karakeep.example.internal/api/v1/assets/abc"),
        # Empty candidate
        ("https://karakeep.example.internal", ""),
    ],
)
def test_non_matching_urls_are_not_trusted(base, candidate):
    assert is_karakeep_trusted_asset_url(candidate, base) is False
