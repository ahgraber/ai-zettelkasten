"""Unit tests for typed errors and retryability classification."""

from __future__ import annotations

import pytest

from aizk.conversion.core.errors import (
    ChainNotTerminated,
    FetcherDepthExceeded,
    FetcherNotRegistered,
    NoConverterForFormat,
    RegistrationRoleMismatch,
)


def test_fetcher_not_registered_is_non_retryable_lookup_error():
    assert issubclass(FetcherNotRegistered, LookupError)
    assert FetcherNotRegistered.retryable is False


def test_no_converter_for_format_is_non_retryable_lookup_error():
    assert issubclass(NoConverterForFormat, LookupError)
    assert NoConverterForFormat.retryable is False


def test_fetcher_depth_exceeded_is_non_retryable_runtime_error():
    assert issubclass(FetcherDepthExceeded, RuntimeError)
    assert FetcherDepthExceeded.retryable is False


def test_fetcher_depth_exceeded_message_contains_cap_chain_and_config_key():
    err = FetcherDepthExceeded(
        cap=2,
        kinds_traversed=("karakeep_bookmark", "arxiv", "url"),
        config_key="AIZK_CONVERTER__DEPTH_CAP",
    )
    msg = str(err)
    assert "2" in msg
    assert "karakeep_bookmark" in msg
    assert "arxiv" in msg
    assert "url" in msg
    assert "AIZK_CONVERTER__DEPTH_CAP" in msg
    assert err.cap == 2
    assert err.kinds_traversed == ("karakeep_bookmark", "arxiv", "url")
    assert err.config_key == "AIZK_CONVERTER__DEPTH_CAP"


def test_chain_not_terminated_is_non_retryable_runtime_error():
    assert issubclass(ChainNotTerminated, RuntimeError)
    assert ChainNotTerminated.retryable is False


def test_registration_role_mismatch_is_non_retryable_type_error():
    assert issubclass(RegistrationRoleMismatch, TypeError)
    assert RegistrationRoleMismatch.retryable is False


def test_fetcher_depth_exceeded_is_raisable_and_catchable():
    with pytest.raises(FetcherDepthExceeded) as exc_info:
        raise FetcherDepthExceeded(
            cap=2,
            kinds_traversed=["karakeep_bookmark"],
            config_key="AIZK_CONVERTER__DEPTH_CAP",
        )
    assert exc_info.value.cap == 2
