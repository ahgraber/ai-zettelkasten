"""Unit tests for the shared fetch helpers in `fetch_helpers.py`.

The arXiv PDF path delegates to `egress_fetch_bytes`. Its exception
handling must preserve typed non-retryable errors so the worker's
retry classification remains correct; only genuinely transient
failures may be wrapped as `ArxivPdfFetchError` (retryable).
"""

from __future__ import annotations

import asyncio

import pytest

from aizk.conversion.core.errors import (
    ArxivPdfFetchError,
    DenyListDestination,
    EgressPolicyError,
    FetchTooLargeError,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.fetch_helpers import fetch_arxiv_pdf


def _run(coro):
    return asyncio.run(coro)


def test_fetch_arxiv_pdf_propagates_fetch_too_large_error_unwrapped(monkeypatch):
    """``FetchTooLargeError`` from the egress helper must surface unchanged.

    Wrapping it as ``ArxivPdfFetchError`` (retryable=True) would flip the
    worker's classification from FAILED_PERM to FAILED_RETRYABLE, causing
    the same oversized PDF to be retried indefinitely.
    """

    async def _fake(url, **kwargs):
        raise FetchTooLargeError("exceeds configured limit of 5 bytes")

    monkeypatch.setattr(
        "aizk.conversion.utilities.fetch_helpers.egress_fetch_bytes",
        _fake,
    )
    # Bypass the rate limiter so this test does not sleep.
    monkeypatch.setattr(
        "aizk.conversion.utilities.fetch_helpers._arxiv_rate_limiter.acquire",
        lambda: asyncio.sleep(0),
    )

    config = ConversionConfig(_env_file=None, fetch_max_response_bytes=5)
    with pytest.raises(FetchTooLargeError, match="exceeds configured limit"):
        _run(fetch_arxiv_pdf("1706.03762", config))


def test_fetch_arxiv_pdf_propagates_egress_policy_error_unwrapped(monkeypatch):
    """An ``EgressPolicyError`` subclass propagates unchanged (existing behavior, pinned)."""

    async def _fake(url, **kwargs):
        raise DenyListDestination("denied")

    monkeypatch.setattr(
        "aizk.conversion.utilities.fetch_helpers.egress_fetch_bytes",
        _fake,
    )
    monkeypatch.setattr(
        "aizk.conversion.utilities.fetch_helpers._arxiv_rate_limiter.acquire",
        lambda: asyncio.sleep(0),
    )

    config = ConversionConfig(_env_file=None)
    with pytest.raises(EgressPolicyError):
        _run(fetch_arxiv_pdf("1706.03762", config))


def test_fetch_arxiv_pdf_wraps_generic_transient_exception_as_retryable(monkeypatch):
    """A generic transient exception is wrapped as ``ArxivPdfFetchError`` (retryable)."""

    async def _fake(url, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(
        "aizk.conversion.utilities.fetch_helpers.egress_fetch_bytes",
        _fake,
    )
    monkeypatch.setattr(
        "aizk.conversion.utilities.fetch_helpers._arxiv_rate_limiter.acquire",
        lambda: asyncio.sleep(0),
    )

    config = ConversionConfig(_env_file=None)
    with pytest.raises(ArxivPdfFetchError, match="connection reset"):
        _run(fetch_arxiv_pdf("1706.03762", config))
