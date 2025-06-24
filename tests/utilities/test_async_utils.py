"""Unit tests for async_utils module.

This module provides comprehensive test coverage for async utility functions,
including event loop management and async task execution from sync contexts.
"""

import asyncio
import logging
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from aizk.utilities.async_utils import (
    is_event_loop_running,
    run_async,
)


@pytest.fixture
def disable_logging():
    """Fixture to disable logging during tests to avoid noise."""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


class TestIsEventLoopRunning:
    """Test cases for is_event_loop_running function."""

    def test_no_event_loop_running(self, disable_logging):
        """Test returns False when no event loop is running."""
        # Arrange & Act
        result = is_event_loop_running()

        # Assert
        assert result is False

    @pytest.mark.asyncio(loop_scope="function")
    async def test_event_loop_running(self, disable_logging):
        """Test returns True when event loop is running."""
        # Arrange & Act
        result = is_event_loop_running()

        # Assert
        assert result is True

    def test_runtime_error_handling(self, disable_logging):
        """Test proper handling when asyncio.get_running_loop raises RuntimeError."""
        # Arrange
        with patch("asyncio.get_running_loop", side_effect=RuntimeError("No running loop")):
            # Act
            result = is_event_loop_running()

            # Assert
            assert result is False


class TestRunAsync:
    """Test cases for run_async function."""

    def test_run_coroutine_directly(self, disable_logging):
        """Test running a coroutine directly."""

        # Arrange
        async def sample_async_func() -> str:
            return "test_result"

        coro = sample_async_func()

        # Act
        result = run_async(coro)

        # Assert
        assert result == "test_result"

    def test_run_async_function_with_args(self, disable_logging):
        """Test running async function with positional arguments."""

        # Arrange
        async def sample_async_func(value: int, multiplier: int = 2) -> int:
            return value * multiplier

        # Act
        result = run_async(sample_async_func, 5, 3)

        # Assert
        assert result == 15

    def test_run_async_function_with_kwargs(self, disable_logging):
        """Test running async function with keyword arguments."""

        # Arrange
        async def sample_async_func(base: int, multiplier: int = 2) -> int:
            return base * multiplier

        # Act
        result = run_async(sample_async_func, base=10, multiplier=4)

        # Assert
        assert result == 40

    def test_run_async_propagates_exceptions(self, disable_logging):
        """Test that exceptions from the coroutine are propagated."""

        # Arrange
        async def failing_async_func():
            raise ValueError("Test exception")

        # Act & Assert
        with pytest.raises(ValueError, match="Test exception"):
            run_async(failing_async_func)

    @patch("IPython.core.getipython.get_ipython")
    def test_run_async_in_jupyter_notebook(self, mock_get_ipython, caplog):
        """Test that a warning is logged in a Jupyter/IPython environment."""

        # Arrange
        mock_get_ipython.return_value = True  # Simulate being in IPython

        async def sample_async_func():
            return "done"

        # Act
        with caplog.at_level(logging.WARNING):
            run_async(sample_async_func)

        # Assert
        assert "run_async is not recommended in Jupyter/IPython" in caplog.text


@pytest.mark.asyncio(loop_scope="function")
class TestRunAsyncInAsyncContext:
    """
    Tests run_async from a sync function executed in a separate thread,
    while an event loop is active in the main thread. This simulates
    calling a sync function that uses run_async from within an async
    application (e.g., a web server, Jupyter/IPython).
    """

    async def test_run_async_from_sync_thread_with_active_loop(self, disable_logging):
        """Tests that run_async works correctly when called from a sync function in a thread."""

        # Arrange
        async def inner_async_func():
            # This coroutine will be scheduled on the main thread's event loop
            await asyncio.sleep(0.01)
            return "inner_result"

        def sync_caller():
            # This function runs in a separate thread without an active event loop.
            # It calls run_async, which should detect the loop in the main
            # thread and schedule the coroutine there.
            return run_async(inner_async_func)

        # Act
        # Run the synchronous caller in a separate thread.
        # await asyncio.to_thread blocks until the thread is complete.
        result = await asyncio.to_thread(sync_caller)

        # Assert
        # The result from the coroutine should be correctly returned.
        assert result == "inner_result"

    async def test_run_async_from_sync_thread_with_args(self, disable_logging):
        """Tests run_async with arguments from a sync thread with an active loop."""

        # Arrange
        async def inner_async_func(value: int):
            await asyncio.sleep(0.01)
            return value * 2

        def sync_caller():
            return run_async(inner_async_func, 10)

        # Act
        result = await asyncio.to_thread(sync_caller)

        # Assert
        assert result == 20
