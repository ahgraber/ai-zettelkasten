"""Tests for typed errors in the core pipeline."""

from __future__ import annotations

import pytest

from aizk.conversion.core.errors import (
    ChainNotTerminated,
    FetcherDepthExceeded,
    FetcherNotRegistered,
    NoConverterForFormat,
)
from aizk.conversion.core.types import ContentType


def test_fetcher_not_registered_carries_kind():
    err = FetcherNotRegistered("singlefile")
    assert err.kind == "singlefile"
    assert err.retryable is False


def test_no_converter_for_format_carries_fields():
    err = NoConverterForFormat(ContentType.IMAGE, "docling")
    assert err.content_type is ContentType.IMAGE
    assert err.name == "docling"
    assert err.retryable is False


def test_fetcher_depth_exceeded_carries_fields():
    err = FetcherDepthExceeded(depth=3, kind="karakeep_bookmark")
    assert err.depth == 3
    assert err.kind == "karakeep_bookmark"
    assert err.retryable is False


def test_chain_not_terminated_with_missing_kind():
    err = ChainNotTerminated(
        "KarakeepResolver declares 'arxiv' but no adapter is registered for it",
        resolver_name="KarakeepBookmarkResolver",
        missing_kind="arxiv",
    )
    assert err.resolver_name == "KarakeepBookmarkResolver"
    assert err.missing_kind == "arxiv"
    assert err.cycle_path is None
    assert err.retryable is False


def test_chain_not_terminated_with_cycle():
    err = ChainNotTerminated(
        "Cycle detected: a -> b -> a",
        cycle_path=["a", "b", "a"],
    )
    assert err.cycle_path == ["a", "b", "a"]
    assert err.resolver_name is None
    assert err.missing_kind is None


def test_chain_not_terminated_message_only():
    err = ChainNotTerminated("generic wiring error")
    assert str(err) == "generic wiring error"
    assert err.resolver_name is None
    assert err.missing_kind is None
    assert err.cycle_path is None
