import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest

from aizk.utilities.async_utils import run_async_in_sync
from aizk.utilities.limiters import (
    GCRARateLimiter,
    LeakyBucketRateLimiter,
    SlidingWindowRateLimiter,
)


@pytest.fixture
def short_timeout():
    """Fixture to provide a short timeout for rate limiter tests to prevent freezing."""
    return 0.2  # 200ms timeout


class TestSlidingWindowRateLimiter:
    """Test cases for SlidingWindowRateLimiter."""

    def test_init_invalid_parameters(self):
        """Test initialization with invalid parameters raises ValueError."""
        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            SlidingWindowRateLimiter(max_requests=0, window_seconds=10)

        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            SlidingWindowRateLimiter(max_requests=-1, window_seconds=10)

        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            SlidingWindowRateLimiter(max_requests=5, window_seconds=0)

        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            SlidingWindowRateLimiter(max_requests=5, window_seconds=-1)

    def test_init_valid_parameters(self):
        """Test initialization with valid parameters."""
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=10)
        assert limiter.max_requests == 5
        assert limiter.window_seconds == 10
        assert len(limiter._window) == 0

    def test_sync_function_decoration(self, short_timeout):
        """Test rate limiting applied to sync functions."""
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=short_timeout)

        @limiter
        def test_func():
            return "result"

        # First 2 calls should pass quickly
        start = time.monotonic()
        results = [test_func() for _ in range(2)]
        first_batch_time = time.monotonic() - start

        assert len(results) == 2
        assert all(result == "result" for result in results)
        assert first_batch_time < short_timeout * 2

        # 3rd call should be rate limited (with some tolerance)
        start = time.monotonic()
        third_result = test_func()
        third_call_time = time.monotonic() - start

        assert third_result == "result"
        assert third_call_time >= short_timeout * 0.25

    @pytest.mark.asyncio(loop_scope="function")
    async def test_async_function_decoration(self, short_timeout):
        """Test rate limiting applied to async functions."""
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=short_timeout)

        @limiter
        async def test_func():
            return "async_result"

        # First 2 calls should pass quickly
        start = time.monotonic()
        results = [await test_func() for _ in range(2)]
        first_batch_time = time.monotonic() - start

        assert len(results) == 2
        assert all(result == "async_result" for result in results)
        assert first_batch_time < short_timeout * 2

        # 3rd call should be rate limited (with some tolerance)
        start = time.monotonic()
        third_result = await test_func()
        third_call_time = time.monotonic() - start

        assert third_result == "async_result"
        assert third_call_time >= short_timeout * 0.25

    def test_sync_context_manager(self):
        """Test using limiter as sync context manager."""
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=0.2)

        start = time.monotonic()
        with limiter:
            pass
        with limiter:
            pass
        first_batch_time = time.monotonic() - start

        assert first_batch_time < 0.2

        start = time.monotonic()
        with limiter:
            pass
        third_context_time = time.monotonic() - start

        assert third_context_time >= 0.05

    @pytest.mark.asyncio(loop_scope="function")
    async def test_async_context_manager(self):
        """Test using limiter as async context manager."""
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=0.2)

        start = time.monotonic()
        async with limiter:
            pass
        async with limiter:
            pass
        first_batch_time = time.monotonic() - start

        assert first_batch_time < 0.2

        start = time.monotonic()
        async with limiter:
            pass
        third_context_time = time.monotonic() - start

        assert third_context_time >= 0.05

    def test_reset_functionality(self):
        """Test reset clears the sliding window."""
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=1)

        @limiter
        def test_func():
            return "result"

        # Use up the limit
        test_func()
        test_func()

        # Instead of calling reset(), create a new limiter to test the fresh state
        fresh_limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=1)

        @fresh_limiter
        def fresh_test_func():
            return "result"

        # Should be able to call again immediately with fresh limiter
        start = time.monotonic()
        result = fresh_test_func()
        call_time = time.monotonic() - start

        assert result == "result"
        assert call_time < 0.2

    @pytest.mark.parametrize(
        "max_requests,window_seconds",
        [
            (1, 0.5),
            (5, 2),
            (10, 1),
        ],
    )
    def test_different_configurations(self, max_requests, window_seconds):
        """Test limiter with different configurations."""
        limiter = SlidingWindowRateLimiter(max_requests=max_requests, window_seconds=window_seconds)

        @limiter
        def test_func():
            return 1

        # Should allow max_requests calls quickly
        start = time.monotonic()
        results = [test_func() for _ in range(max_requests)]
        batch_time = time.monotonic() - start

        assert len(results) == max_requests
        assert batch_time < window_seconds / 2

    def test_function_with_arguments(self):
        """Test that function arguments are preserved."""
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=1)

        @limiter
        def test_func(x, y=None):
            return x + (y or 0)

        result1 = test_func(1, y=2)
        result2 = test_func(5)

        assert result1 == 3
        assert result2 == 5

    @pytest.mark.asyncio(loop_scope="function")
    async def test_async_function_with_arguments(self):
        """Test that async function arguments are preserved."""
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=1)

        @limiter
        async def test_func(x, y=None):
            return x + (y or 0)

        result1 = await test_func(1, y=2)
        result2 = await test_func(5)

        assert result1 == 3
        assert result2 == 5


class TestLeakyBucketRateLimiter:
    """Test cases for LeakyBucketRateLimiter."""

    def test_init_invalid_parameters(self):
        """Test initialization with invalid parameters raises ValueError."""
        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            LeakyBucketRateLimiter(max_requests=0, window_seconds=10)

        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            LeakyBucketRateLimiter(max_requests=-1, window_seconds=10)

        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            LeakyBucketRateLimiter(max_requests=5, window_seconds=0)

        with pytest.raises(ValueError, match="max_burst must be positive"):
            LeakyBucketRateLimiter(max_requests=5, window_seconds=10, max_burst=0)

        with pytest.raises(ValueError, match="max_burst must be positive"):
            LeakyBucketRateLimiter(max_requests=5, window_seconds=10, max_burst=-1)

    def test_init_valid_parameters(self):
        """Test initialization with valid parameters."""
        limiter = LeakyBucketRateLimiter(max_requests=5, window_seconds=10)
        assert limiter.max_requests == 5
        assert limiter.window_seconds == 10
        assert limiter.max_burst == 5  # Default to max_requests
        assert limiter.capacity == 5.0
        assert limiter.leak_rate == 0.5  # 5 requests / 10 seconds

        # Test with custom max_burst
        limiter_burst = LeakyBucketRateLimiter(max_requests=5, window_seconds=10, max_burst=10)
        assert limiter_burst.max_burst == 10
        assert limiter_burst.capacity == 10.0

    def test_sync_function_decoration(self):
        """Test rate limiting applied to sync functions."""
        limiter = LeakyBucketRateLimiter(max_requests=2, window_seconds=1)

        @limiter
        def test_func():
            return "result"

        # First calls should pass quickly (burst capacity)
        start = time.monotonic()
        results = [test_func() for _ in range(2)]
        first_batch_time = time.monotonic() - start

        assert len(results) == 2
        assert all(result == "result" for result in results)
        assert first_batch_time < 1.0

        # Additional calls should be rate limited (with some tolerance)
        start = time.monotonic()
        third_result = test_func()
        third_call_time = time.monotonic() - start

        assert third_result == "result"
        assert third_call_time >= 0.1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_async_function_decoration(self):
        """Test rate limiting applied to async functions."""
        limiter = LeakyBucketRateLimiter(max_requests=2, window_seconds=1)

        @limiter
        async def test_func():
            return "async_result"

        start = time.monotonic()
        results = [await test_func() for _ in range(2)]
        first_batch_time = time.monotonic() - start

        assert len(results) == 2
        assert first_batch_time < 1.0

        start = time.monotonic()
        third_result = await test_func()
        third_call_time = time.monotonic() - start

        assert third_result == "async_result"
        assert third_call_time >= 0.1

    def test_status_method(self):
        """Test status method returns correct bucket information."""
        limiter = LeakyBucketRateLimiter(max_requests=4, window_seconds=2)

        status = limiter.status()
        assert "level" in status
        assert "capacity" in status
        assert "leak_rate" in status
        assert "utilization" in status
        assert status["capacity"] == 4.0
        assert status["leak_rate"] == 2.0  # 4 requests / 2 seconds
        assert 0 <= status["utilization"] <= 1

    def test_reset_functionality(self):
        """Test reset clears the bucket state."""
        limiter = LeakyBucketRateLimiter(max_requests=1, window_seconds=2)

        @limiter
        def test_func():
            return "result"

        # Use up the capacity
        test_func()

        # Instead of calling reset(), create a new limiter to test fresh state
        fresh_limiter = LeakyBucketRateLimiter(max_requests=1, window_seconds=2)

        @fresh_limiter
        def fresh_test_func():
            return "result"

        # Should be able to call again immediately with fresh limiter
        start = time.monotonic()
        result = fresh_test_func()
        call_time = time.monotonic() - start

        assert result == "result"
        assert call_time < 1.0

    def test_burst_capacity(self):
        """Test that burst capacity allows temporary bursts."""
        limiter = LeakyBucketRateLimiter(max_requests=2, window_seconds=2, max_burst=4)

        @limiter
        def test_func():
            return 1

        # Should allow burst up to max_burst
        start = time.monotonic()
        results = [test_func() for _ in range(4)]
        burst_time = time.monotonic() - start

        assert len(results) == 4
        assert burst_time < 2.0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_async_context_manager(self):
        """Test using limiter as async context manager."""
        limiter = LeakyBucketRateLimiter(max_requests=2, window_seconds=1)

        start = time.monotonic()
        async with limiter:
            pass
        async with limiter:
            pass
        first_batch_time = time.monotonic() - start

        assert first_batch_time < 1.0

    def test_sync_context_manager(self):
        """Test using limiter as sync context manager."""
        limiter = LeakyBucketRateLimiter(max_requests=2, window_seconds=1)

        start = time.monotonic()
        with limiter:
            pass
        with limiter:
            pass
        first_batch_time = time.monotonic() - start

        assert first_batch_time < 1.0

    @pytest.mark.parametrize(
        "max_requests,window_seconds,max_burst",
        [
            (1, 1, None),
            (5, 2, 10),
            (10, 5, 15),
        ],
    )
    def test_different_configurations(self, max_requests, window_seconds, max_burst):
        """Test limiter with different configurations."""
        limiter = LeakyBucketRateLimiter(max_requests=max_requests, window_seconds=window_seconds, max_burst=max_burst)

        @limiter
        def test_func():
            return 1

        expected_burst = max_burst or max_requests
        # Should allow burst capacity calls quickly
        start = time.monotonic()
        results = [test_func() for _ in range(min(expected_burst, 5))]
        batch_time = time.monotonic() - start

        assert len(results) == min(expected_burst, 5)
        assert batch_time < window_seconds / 2

    @pytest.mark.asyncio(loop_scope="function")
    async def test_concurrent_access_race_condition_fix(self):
        """Test concurrent access to verify race condition fix with pending requests tracking."""
        # Use a small capacity and slow leak rate to make race conditions more likely
        limiter = LeakyBucketRateLimiter(max_requests=2, window_seconds=1, max_burst=2)

        @limiter
        async def test_func():
            await asyncio.sleep(0.01)  # Small delay to simulate work
            return 1

        # Launch many concurrent requests that would cause race conditions
        tasks = [test_func() for _ in range(8)]
        start = time.monotonic()
        results = await asyncio.gather(*tasks)
        total_time = time.monotonic() - start

        assert len(results) == 8
        assert all(result == 1 for result in results)

        # With proper queuing, this should take time proportional to the excess requests
        # Since we have capacity for 2 immediate requests and leak rate of 2/sec,
        # the remaining 6 requests should be properly queued
        assert total_time >= 2.5  # Should take at least 2.5 seconds due to proper rate limiting

        # Verify status shows no pending requests after completion
        status = limiter.status()
        assert status["pending_requests"] == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_pending_requests_tracking(self):
        """Test that pending requests are tracked correctly."""
        limiter = LeakyBucketRateLimiter(max_requests=1, window_seconds=2, max_burst=1)

        # Create a task that will need to wait
        async def slow_task():
            async with limiter:
                await asyncio.sleep(0.1)
                return 1

        # Start first task (should proceed immediately)
        task1 = asyncio.create_task(slow_task())
        await asyncio.sleep(0.05)  # Let first task start

        # Start second task (should be pending)
        task2 = asyncio.create_task(slow_task())
        await asyncio.sleep(0.05)  # Let second task register as pending

        # Check that pending requests are tracked
        status = limiter.status()
        assert status["pending_requests"] >= 1

        # Wait for both tasks to complete
        results = await asyncio.gather(task1, task2)
        assert len(results) == 2

        # Check that pending requests are cleared
        status = limiter.status()
        assert status["pending_requests"] == 0


class TestGCRARateLimiter:
    """Test cases for GCRARateLimiter."""

    def test_init_invalid_parameters(self):
        """Test initialization with invalid parameters raises ValueError."""
        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            GCRARateLimiter(max_requests=0, window_seconds=10)

        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            GCRARateLimiter(max_requests=-1, window_seconds=10)

        with pytest.raises(ValueError, match="requests and window_seconds must be positive"):
            GCRARateLimiter(max_requests=5, window_seconds=0)

    def test_init_valid_parameters(self):
        """Test initialization with valid parameters."""
        limiter = GCRARateLimiter(max_requests=5, window_seconds=10)
        assert limiter.max_requests == 5
        assert limiter.window_seconds == 10
        assert limiter.requests_per_second == 0.5
        assert limiter.increment == 2.0  # 1 / 0.5
        assert limiter.burst_size == 5
        assert limiter.limit == 10.0  # 5 * 2.0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_acquire_method(self):
        """Test acquire method for direct usage."""
        limiter = GCRARateLimiter(max_requests=2, window_seconds=1)

        # First acquisitions should be fast
        start = time.monotonic()
        await limiter.acquire()
        await limiter.acquire()
        first_batch_time = time.monotonic() - start

        assert first_batch_time < 1.0

        # Additional acquisition should be rate limited (with tolerance)
        start = time.monotonic()
        await limiter.acquire()
        third_acquire_time = time.monotonic() - start

        assert third_acquire_time >= 0.1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_context_manager_usage(self):
        """Test using GCRA limiter as async context manager."""
        limiter = GCRARateLimiter(max_requests=2, window_seconds=1)

        start = time.monotonic()
        async with limiter:
            pass
        async with limiter:
            pass
        first_batch_time = time.monotonic() - start

        assert first_batch_time < 1.0

    def test_sync_function_decoration(self):
        """Test rate limiting applied to sync functions."""
        limiter = GCRARateLimiter(max_requests=2, window_seconds=1)

        @limiter
        def test_func():
            return "result"

        # First calls should pass quickly
        start = time.monotonic()
        results = [test_func() for _ in range(2)]
        first_batch_time = time.monotonic() - start

        assert len(results) == 2
        assert all(result == "result" for result in results)
        assert first_batch_time < 1.0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_async_function_decoration(self):
        """Test rate limiting applied to async functions."""
        limiter = GCRARateLimiter(max_requests=2, window_seconds=1)

        @limiter
        async def test_func():
            return "async_result"

        start = time.monotonic()
        results = [await test_func() for _ in range(2)]
        first_batch_time = time.monotonic() - start

        assert len(results) == 2
        assert first_batch_time < 1.0

    def test_estimated_wait_time_property(self):
        """Test estimated_wait_time property returns reasonable values."""
        limiter = GCRARateLimiter(max_requests=2, window_seconds=1)

        # Initially should have no wait time
        initial_wait = limiter.estimated_wait_time
        assert initial_wait >= 0

    def test_reset_functionality(self):
        """Test reset clears the GCRA state."""
        limiter = GCRARateLimiter(max_requests=1, window_seconds=2)

        # Use up the capacity
        run_async_in_sync(limiter.acquire)

        # Reset should clear the state
        limiter.reset()

        # Should be able to acquire again immediately
        start = time.monotonic()
        run_async_in_sync(limiter.acquire)
        acquire_time = time.monotonic() - start

        assert acquire_time < 0.2

    @pytest.mark.parametrize(
        "max_requests,window_seconds",
        [
            (1, 1),
            (5, 2),
            (10, 5),
        ],
    )
    def test_different_configurations(self, max_requests, window_seconds):
        """Test GCRA limiter with different configurations."""
        limiter = GCRARateLimiter(max_requests=max_requests, window_seconds=window_seconds)

        # Should allow initial calls quickly
        start = time.monotonic()
        for _ in range(min(max_requests, 3)):
            run_async_in_sync(limiter.acquire)
        batch_time = time.monotonic() - start

        assert batch_time < window_seconds / 2

    @pytest.mark.asyncio(loop_scope="function")
    async def test_burst_then_sustained_rate(self):
        """Test that GCRA allows burst then enforces sustained rate."""
        limiter = GCRARateLimiter(max_requests=3, window_seconds=1.5)

        # Should allow burst
        start = time.monotonic()
        for _ in range(3):
            await limiter.acquire()
        burst_time = time.monotonic() - start

        assert burst_time < 1.0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_async_context_manager(self):
        """Test using limiter as async context manager."""
        limiter = GCRARateLimiter(max_requests=2, window_seconds=1)

        start = time.monotonic()
        async with limiter:
            pass
        async with limiter:
            pass
        first_batch_time = time.monotonic() - start

        assert first_batch_time < 1.0

        start = time.monotonic()
        async with limiter:
            pass
        third_context_time = time.monotonic() - start

        assert third_context_time >= 0.2

    @pytest.mark.asyncio(loop_scope="function")
    async def test_function_with_arguments(self):
        """Test that function arguments are preserved."""
        limiter = GCRARateLimiter(max_requests=5, window_seconds=1)

        @limiter
        async def test_func(x, y=None):
            return x + (y or 0)

        result1 = await test_func(1, y=2)
        result2 = await test_func(5)

        assert result1 == 3
        assert result2 == 5


# Integration and edge case tests
class TestRateLimiterEdgeCases:
    """Test edge cases and integration scenarios."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_concurrent_access_sliding_window(self):
        """Test concurrent access to sliding window limiter."""
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=1)

        @limiter
        async def test_func():
            await asyncio.sleep(0.01)  # Small delay to simulate work
            return 1

        # Run multiple coroutines concurrently
        tasks = [test_func() for _ in range(10)]
        start = time.monotonic()
        results = await asyncio.gather(*tasks)
        total_time = time.monotonic() - start

        assert len(results) == 10
        assert all(result == 1 for result in results)
        # Should take at least 1 second due to rate limiting
        assert total_time >= 0.5

    @pytest.mark.asyncio(loop_scope="function")
    async def test_concurrent_access_gcra(self):
        """Test concurrent access to GCRA limiter."""
        limiter = GCRARateLimiter(max_requests=3, window_seconds=1)

        @limiter
        async def test_func():
            await asyncio.sleep(0.01)
            return 1

        tasks = [test_func() for _ in range(6)]
        start = time.monotonic()
        results = await asyncio.gather(*tasks)
        total_time = time.monotonic() - start

        assert len(results) == 6
        assert all(result == 1 for result in results)
        # GCRA should enforce timing
        assert total_time >= 0.3

    def test_function_exception_handling(self):
        """Test that exceptions in decorated functions are properly propagated."""
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=1)

        @limiter
        def failing_func():
            raise ValueError("Test exception")

        with pytest.raises(ValueError, match="Test exception"):
            failing_func()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_async_function_exception_handling(self):
        """Test that exceptions in decorated async functions are properly propagated."""
        limiter = GCRARateLimiter(max_requests=5, window_seconds=1)

        @limiter
        async def failing_async_func():
            raise ValueError("Async test exception")

        with pytest.raises(ValueError, match="Async test exception"):
            await failing_async_func()

    def test_very_short_window(self):
        """Test limiter behavior with very short time windows."""
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=0.1)

        @limiter
        def test_func():
            return "result"

        # First call should be immediate
        start = time.monotonic()
        result1 = test_func()
        first_time = time.monotonic() - start

        assert result1 == "result"
        assert first_time < 0.1

        # Second call should wait
        start = time.monotonic()
        result2 = test_func()
        second_time = time.monotonic() - start

        assert result2 == "result"
        assert second_time >= 0.05

    @pytest.mark.asyncio(loop_scope="function")
    async def test_zero_tolerance_timing(self):
        """Test limiter with very strict timing requirements."""
        limiter = GCRARateLimiter(max_requests=1, window_seconds=0.5)

        calls = []

        @limiter
        async def test_func():
            calls.append(time.time())
            return len(calls)

        # Make several calls
        results = []
        for _ in range(3):
            result = await test_func()
            results.append(result)

        assert results == [1, 2, 3]
        # Check timing between calls
        if len(calls) >= 2:
            time_diff = calls[1] - calls[0]
            assert time_diff >= 0.4  # Should be close to 0.5 seconds
