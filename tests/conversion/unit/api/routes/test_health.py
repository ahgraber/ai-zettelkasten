"""Unit tests for health check async functions with task-leak detection.

Verifies that _check_db and _check_s3 do not leak asyncio tasks, even
when the underlying blocking call times out or raises.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
from pyleak import no_task_leaks
import pytest

from aizk.conversion.api.routes.health import _check_db, _check_picture_description, _check_s3
from aizk.conversion.utilities.config import DoclingConverterConfig


@pytest.mark.asyncio
async def test_check_db_ok_no_task_leaks() -> None:
    """Successful DB check completes without leaking tasks."""
    engine = MagicMock()
    engine.connect.return_value.__enter__ = MagicMock()
    engine.connect.return_value.__exit__ = MagicMock(return_value=False)

    async with no_task_leaks(action="raise"):
        result = await _check_db(engine)

    assert result.name == "database"
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_check_db_error_no_task_leaks() -> None:
    """Failed DB check completes without leaking tasks."""
    engine = MagicMock()
    engine.connect.side_effect = Exception("connection refused")

    async with no_task_leaks(action="raise"):
        result = await _check_db(engine)

    assert result.status == "unavailable"
    assert "connection refused" in result.detail


@pytest.mark.asyncio
async def test_check_db_timeout_no_task_leaks() -> None:
    """DB check that exceeds timeout completes without leaking tasks."""
    engine = MagicMock()

    def _slow_connect():
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(side_effect=lambda: time.sleep(10))
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    engine.connect = _slow_connect

    with patch("aizk.conversion.api.routes.health._CHECK_TIMEOUT_SECONDS", 0.5):
        async with no_task_leaks(action="raise"):
            result = await _check_db(engine)

    assert result.status == "unavailable"
    assert result.detail == "timeout"


@pytest.mark.asyncio
async def test_check_s3_ok_no_task_leaks() -> None:
    """Successful S3 check completes without leaking tasks."""
    s3_client = MagicMock()
    s3_client.config.s3_bucket_name = "test-bucket"

    async with no_task_leaks(action="raise"):
        result = await _check_s3(s3_client)

    assert result.name == "s3"
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_check_s3_error_no_task_leaks() -> None:
    """Failed S3 check completes without leaking tasks."""
    s3_client = MagicMock()
    s3_client.config.s3_bucket_name = "test-bucket"
    s3_client.client.head_bucket.side_effect = Exception("access denied")

    async with no_task_leaks(action="raise"):
        result = await _check_s3(s3_client)

    assert result.status == "unavailable"
    assert "access denied" in result.detail


@pytest.mark.asyncio
async def test_check_s3_timeout_no_task_leaks() -> None:
    """S3 check that exceeds timeout completes without leaking tasks."""
    s3_client = MagicMock()
    s3_client.config.s3_bucket_name = "test-bucket"
    s3_client.client.head_bucket.side_effect = lambda **_kw: time.sleep(10)

    with patch("aizk.conversion.api.routes.health._CHECK_TIMEOUT_SECONDS", 0.5):
        async with no_task_leaks(action="raise"):
            result = await _check_s3(s3_client)

    assert result.status == "unavailable"
    assert result.detail == "timeout"


def _make_picture_description_config(
    base_url: str = "http://vllm.local/v1", api_key: str = "key"
) -> DoclingConverterConfig:
    return DoclingConverterConfig(
        picture_description_base_url=base_url,
        picture_description_api_key=api_key,
        _env_file=None,
    )


@pytest.mark.asyncio
async def test_check_picture_description_ok() -> None:
    config = _make_picture_description_config()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    with patch("aizk.conversion.api.routes.health.httpx.get", return_value=mock_response):
        result = await _check_picture_description(config)
    assert result.name == "picture_description"
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_check_picture_description_unavailable_on_non_2xx() -> None:
    config = _make_picture_description_config()
    with patch(
        "aizk.conversion.api.routes.health.httpx.get",
        side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock()),
    ):
        result = await _check_picture_description(config)
    assert result.name == "picture_description"
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_check_picture_description_unavailable_on_connection_error() -> None:
    config = _make_picture_description_config()
    with patch(
        "aizk.conversion.api.routes.health.httpx.get",
        side_effect=httpx.ConnectError("refused"),
    ):
        result = await _check_picture_description(config)
    assert result.name == "picture_description"
    assert result.status == "unavailable"
