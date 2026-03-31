"""Unit tests for health check async functions with task-leak detection.

Verifies that _check_db and _check_s3 do not leak asyncio tasks, even
when the underlying blocking call times out or raises.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from pyleak import no_task_leaks
import pytest

from aizk.conversion.api.routes.health import _check_db, _check_s3


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

    with patch("aizk.conversion.api.routes.health._CHECK_TIMEOUT_SECONDS", 0.05):
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

    with patch("aizk.conversion.api.routes.health._CHECK_TIMEOUT_SECONDS", 0.05):
        async with no_task_leaks(action="raise"):
            result = await _check_s3(s3_client)

    assert result.status == "unavailable"
    assert result.detail == "timeout"
