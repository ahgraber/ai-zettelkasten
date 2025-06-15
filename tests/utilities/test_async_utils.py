"""Unit tests for async_helpers module.

This module provides comprehensive test coverage for async utility functions,
including event loop management, context validation, and async task execution.
"""

import asyncio
import logging
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from aizk.utilities.async_utils import (
    is_event_loop_running,
    run_async_in_sync,
    run_async_tasks,
    validate_sync_context_for_asyncio,
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


class TestValidateSyncContextForAsyncio:
    """Test cases for validate_sync_context_for_asyncio function."""

    def test_valid_sync_context(self, disable_logging):
        """Test validation passes in proper sync context."""
        # Arrange & Act & Assert - should not raise any exception
        validate_sync_context_for_asyncio()

    def test_jupyter_environment_detection(self, disable_logging):
        """Test raises RuntimeError when called from Jupyter/IPython."""
        # Arrange
        mock_ipython = MagicMock()

        with patch("IPython.core.getipython.get_ipython", return_value=mock_ipython):
            # Act & Assert
            with pytest.raises(RuntimeError) as exc_info:
                validate_sync_context_for_asyncio()

            assert "Jupyter/IPython" in str(exc_info.value)
            assert "await your_async_function()" in str(exc_info.value)

    def test_jupyter_import_error_handling(self, disable_logging):
        """Test proper handling when IPython is not available."""
        # Arrange
        with patch("IPython.core.getipython.get_ipython", side_effect=ImportError("IPython not available")):
            # Act & Assert - should not raise exception and continue validation
            validate_sync_context_for_asyncio()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_async_context_detection(self, disable_logging):
        """Test raises RuntimeError when called from async context."""
        # Arrange
        with patch("IPython.core.getipython.get_ipython", side_effect=ImportError("IPython not available")):
            # Act & Assert
            with pytest.raises(RuntimeError) as exc_info:
                validate_sync_context_for_asyncio()

            assert "Cannot use asyncio.run() from within an async context" in str(exc_info.value)
            assert "Use 'await' directly" in str(exc_info.value)


class TestRunAsyncInSync:
    """Test cases for run_async_in_sync function."""

    def test_run_coroutine_directly(self, disable_logging):
        """Test running a coroutine directly."""

        # Arrange
        async def sample_async_func() -> str:
            return "test_result"

        coro = sample_async_func()

        # Act
        result = run_async_in_sync(coro)

        # Assert
        assert result == "test_result"

    def test_run_async_function_with_args(self, disable_logging):
        """Test running async function with positional arguments."""

        # Arrange
        async def sample_async_func(value: int, multiplier: int = 2) -> int:
            return value * multiplier

        # Act
        result = run_async_in_sync(sample_async_func, 5, 3)

        # Assert
        assert result == 15

    def test_run_async_function_with_kwargs(self, disable_logging):
        """Test running async function with keyword arguments."""

        # Arrange
        async def sample_async_func(base: int, multiplier: int = 2) -> int:
            return base * multiplier

        # Act
        result = run_async_in_sync(sample_async_func, base=10, multiplier=4)

        # Assert
        assert result == 40

    def test_run_async_function_mixed_args(self, disable_logging):
        """Test running async function with mixed positional and keyword arguments."""

        # Arrange
        async def sample_async_func(a: int, b: int, multiplier: int = 1) -> int:
            return a + b + multiplier

        # Act
        result = run_async_in_sync(sample_async_func, 5, b=10, multiplier=3)

        # Assert
        assert result == 18

    def test_exception_propagation(self, disable_logging):
        """Test that exceptions from async functions are properly propagated."""

        # Arrange
        async def failing_async_func() -> None:
            raise ValueError("Test error message")

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            run_async_in_sync(failing_async_func)

        assert "Test error message" in str(exc_info.value)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fails_in_async_context(self, disable_logging):
        """Test that function fails when called from async context."""

        # Arrange
        async def sample_async_func() -> str:
            return "result"

        coro = sample_async_func()

        # Act & Assert
        with pytest.raises(RuntimeError) as exc_info:
            run_async_in_sync(coro)

        assert "asyncio.run() cannot be called from a running event loop" in str(exc_info.value)

    def test_complex_async_operation(self, disable_logging):
        """Test with a more complex async operation involving delays."""

        # Arrange
        async def complex_async_func(delay: float, result: str) -> str:
            await asyncio.sleep(delay)
            return f"completed: {result}"

        # Act
        result = run_async_in_sync(complex_async_func, 0.01, "test")

        # Assert
        assert result == "completed: test"


class TestRunAsyncTasks:
    """Test cases for run_async_tasks function."""

    def test_empty_tasks_sequence(self, disable_logging):
        """Test raises ValueError for empty tasks sequence."""
        # Arrange
        tasks = []

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            run_async_tasks(tasks)

        assert "Tasks sequence cannot be empty" in str(exc_info.value)

    def test_single_task_execution(self, disable_logging):
        """Test execution of a single async task."""

        # Arrange
        async def single_task() -> str:
            return "single_result"

        tasks = [single_task()]

        # Act
        results = run_async_tasks(tasks, show_progress=False)

        # Assert
        assert len(results) == 1
        assert results[0] == "single_result"

    def test_multiple_tasks_execution(self, disable_logging):
        """Test concurrent execution of multiple async tasks."""

        # Arrange
        async def numbered_task(number: int) -> str:
            await asyncio.sleep(0.01)  # Small delay to simulate work
            return f"task_{number}"

        tasks = [numbered_task(i) for i in range(5)]

        # Act
        results = run_async_tasks(tasks, show_progress=False)

        # Assert
        assert len(results) == 5
        expected_results = [f"task_{i}" for i in range(5)]
        assert results == expected_results

    def test_task_order_preservation(self, disable_logging):
        """Test that results maintain the same order as input tasks."""

        # Arrange
        async def delay_task(delay: float, value: int) -> int:
            await asyncio.sleep(delay)
            return value

        # Tasks with different delays but should return in input order
        tasks = [
            delay_task(0.03, 1),  # Longest delay
            delay_task(0.01, 2),  # Shortest delay
            delay_task(0.02, 3),  # Medium delay
        ]

        # Act
        results = run_async_tasks(tasks, show_progress=False)

        # Assert
        assert results == [1, 2, 3]  # Should maintain input order

    def test_exception_handling_in_tasks(self, disable_logging):
        """Test proper exception handling when tasks fail."""

        # Arrange
        async def failing_task() -> None:
            raise ValueError("Task failed")

        async def successful_task() -> str:
            return "success"

        tasks = [successful_task(), failing_task()]

        # Act & Assert
        with pytest.raises(ValueError) as exc_info:
            run_async_tasks(tasks, show_progress=False)

        assert "Task failed" in str(exc_info.value)

    @patch("tqdm.asyncio.tqdm.gather")
    def test_progress_bar_enabled(self, mock_gather, disable_logging):
        """Test progress bar functionality when enabled."""

        # Arrange
        async def simple_task(value: int) -> int:
            return value

        tasks = [simple_task(i) for i in range(3)]

        # Mock tqdm.gather to return expected results
        mock_gather.return_value = [0, 1, 2]

        # Act
        results = run_async_tasks(tasks, show_progress=True, progress_bar_desc="Test Tasks")

        # Assert
        assert len(results) == 3
        assert results == [0, 1, 2]
        # Verify tqdm.gather was called
        mock_gather.assert_called_once()

    def test_progress_bar_disabled(self, disable_logging):
        """Test execution without progress bar."""

        # Arrange
        async def simple_task(value: int) -> int:
            return value * 2

        tasks = [simple_task(i) for i in range(3)]

        # Act
        results = run_async_tasks(tasks, show_progress=False)

        # Assert
        assert len(results) == 3
        assert results == [0, 2, 4]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fails_in_async_context(self, disable_logging):
        """Test that function fails when called from async context."""

        # Arrange
        async def sample_task() -> str:
            return "result"

        tasks = [sample_task()]

        # Act & Assert
        with pytest.raises(RuntimeError) as exc_info:
            run_async_tasks(tasks)

        assert "asyncio.run() cannot be called from a running event loop" in str(exc_info.value)

    @pytest.mark.parametrize(
        "task_count,expected_length",
        [
            (1, 1),
            (5, 5),
            (10, 10),
            (50, 50),
        ],
    )
    def test_various_task_counts(self, task_count: int, expected_length: int, disable_logging):
        """Test execution with various numbers of tasks."""

        # Arrange
        async def indexed_task(index: int) -> int:
            return index**2

        tasks = [indexed_task(i) for i in range(task_count)]

        # Act
        results = run_async_tasks(tasks, show_progress=False)

        # Assert
        assert len(results) == expected_length
        expected_results = [i**2 for i in range(task_count)]
        assert results == expected_results

    def test_mixed_return_types(self, disable_logging):
        """Test tasks returning different types."""

        # Arrange
        async def string_task() -> str:
            return "string_result"

        async def int_task() -> int:
            return 42

        async def list_task() -> List[int]:
            return [1, 2, 3]

        async def none_task() -> None:
            return None

        tasks = [string_task(), int_task(), list_task(), none_task()]

        # Act
        results = run_async_tasks(tasks, show_progress=False)

        # Assert
        assert len(results) == 4
        assert results[0] == "string_result"
        assert results[1] == 42
        assert results[2] == [1, 2, 3]
        assert results[3] is None


class TestAsyncHelpersIntegration:
    """Integration tests for async_helpers module."""

    def test_nested_async_operations(self, disable_logging):
        """Test running async operations that internally use async/await."""

        # Arrange
        async def fetch_data(url: str) -> str:
            await asyncio.sleep(0.01)
            return f"data_from_{url}"

        async def process_data(data: str) -> str:
            await asyncio.sleep(0.01)
            return f"processed_{data}"

        async def complex_operation(url: str) -> str:
            raw_data = await fetch_data(url)
            return await process_data(raw_data)

        # Act
        result = run_async_in_sync(complex_operation, "test_url")

        # Assert
        assert result == "processed_data_from_test_url"

    def test_combining_single_and_multiple_task_execution(self, disable_logging):
        """Test using both run_async_in_sync and run_async_tasks."""

        # Arrange
        async def prepare_data() -> List[str]:
            return ["item1", "item2", "item3"]

        async def process_item(item: str) -> str:
            await asyncio.sleep(0.01)
            return f"processed_{item}"

        # Act
        # First get the data using single async operation
        data = run_async_in_sync(prepare_data)

        # Then process all items concurrently
        tasks = [process_item(item) for item in data]
        results = run_async_tasks(tasks, show_progress=False)

        # Assert
        assert len(results) == 3
        expected = ["processed_item1", "processed_item2", "processed_item3"]
        assert results == expected

    def test_error_handling_across_functions(self, disable_logging):
        """Test error handling consistency across different helper functions."""

        # Arrange
        async def always_fails() -> None:
            raise ConnectionError("Network error")

        # Test single task error handling
        with pytest.raises(ConnectionError):
            run_async_in_sync(always_fails)

        # Test multiple tasks error handling
        tasks = [always_fails(), always_fails()]
        with pytest.raises(ConnectionError):
            run_async_tasks(tasks, show_progress=False)
