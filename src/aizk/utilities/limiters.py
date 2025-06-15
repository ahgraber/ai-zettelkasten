import asyncio
from collections import deque
from functools import wraps
import inspect
import time
from typing import Any, Callable, Dict, Optional

from aizk.utilities.async_utils import run_async_in_sync


def _create_sync_async_wrapper(rate_limit_func: Callable, original_func: Callable) -> Callable:
    """Create a wrapper that handles both sync and async functions with rate limiting.

    Args:
        rate_limit_func: The async rate limiting function to call before execution
        original_func: The original function being wrapped

    Returns:
        Appropriately wrapped sync or async function
    """
    if inspect.iscoroutinefunction(original_func):

        @wraps(original_func)
        async def async_wrapper(*args, **kwargs) -> Any:
            await rate_limit_func()
            return await original_func(*args, **kwargs)

        return async_wrapper
    else:

        @wraps(original_func)
        def sync_wrapper(*args, **kwargs) -> Any:
            run_async_in_sync(rate_limit_func)
            return original_func(*args, **kwargs)

        return sync_wrapper


class SlidingWindowRateLimiter:
    """A sliding time-window rate limiter for both sync and async functions.

    This rate limiter maintains a sliding window of request timestamps
    and blocks new requests when the maximum number of requests in the
    time window has been reached.

    Args:
        max_requests: Maximum number of requests allowed in the time window.
        window_seconds: Time window duration in seconds.

    Raises:
        ValueError: If requests or window_seconds are not positive.

    Example:
        >>> @SlidingWindowRateLimiter(max_requests=5, window_seconds=60)
        >>> async def async_api_call():
        ...     return await some_api()

        >>> @SlidingWindowRateLimiter(max_requests=5, window_seconds=60)
        >>> def sync_api_call():
        ...     return requests.get("https://api.example.com")
    """

    def __init__(self, max_requests: int, window_seconds: float):
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("requests and window_seconds must be positive")

        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._window = deque()
        self._pending_requests = 0  # Track pending requests to prevent race conditions
        self._lock = asyncio.Lock()

    def __call__(self, func: Callable) -> Callable:
        """Apply rate limiting to a function.

        Args:
            func: The function to be rate limited (sync or async).

        Returns:
            The wrapped function with rate limiting applied.
        """
        return _create_sync_async_wrapper(self._check_rate_limit, func)

    async def _check_rate_limit(self) -> None:
        """Check if request is within rate limit, block if necessary."""
        async with self._lock:
            current_time = time.time()

            # Remove expired timestamps
            while self._window and self._window[0] <= current_time - self.window_seconds:
                self._window.popleft()

            # Check if we can proceed immediately
            if len(self._window) < self.max_requests:
                self._window.append(current_time)
                return

            # Calculate wait time until oldest request expires
            # Account for pending requests to ensure proper queuing
            oldest_request = self._window[0]
            base_wait_time = self.window_seconds - (current_time - oldest_request)

            # Add additional wait time for pending requests to ensure proper ordering
            additional_wait = self._pending_requests * (self.window_seconds / self.max_requests)
            wait_time = base_wait_time + additional_wait
            self._pending_requests += 1

        # Wait outside the lock
        if wait_time > 0:
            await asyncio.sleep(wait_time)

        # After waiting, add the request
        async with self._lock:
            current_time = time.time()
            # Remove expired timestamps again
            while self._window and self._window[0] <= current_time - self.window_seconds:
                self._window.popleft()
            self._window.append(current_time)
            self._pending_requests = max(0, self._pending_requests - 1)

    def reset(self) -> None:
        """Reset the rate limiter window."""

        # Use run_async_in_sync instead of asyncio.Runner to avoid deadlocks
        async def _reset():
            async with self._lock:
                self._window.clear()
                self._pending_requests = 0

        run_async_in_sync(_reset)

    def __enter__(self) -> "SlidingWindowRateLimiter":
        """Enter the sync context manager, applying rate limiting."""
        run_async_in_sync(self._check_rate_limit)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the sync context manager."""
        pass

    async def __aenter__(self) -> "SlidingWindowRateLimiter":
        """Enter the async context manager, applying rate limiting."""
        await self._check_rate_limit()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the async context manager."""
        pass


class LeakyBucketRateLimiter:
    """A leaky bucket rate limiter for both sync and async functions.

    The bucket fills with each request and leaks at a constant rate.
    Requests are blocked when the bucket would overflow.

    Args:
        max_requests: Maximum number of requests allowed in the time window.
        window_seconds: Time window duration in seconds.
        max_burst: Maximum burst capacity. If None, defaults to max_requests.
                  This allows for temporary bursts above the sustained rate.

    Raises:
        ValueError: If requests, window_seconds, or max_burst are not positive.

    Example:
        >>> # Standard rate limiting: 10 requests per 5 seconds, burst = 10
        >>> @LeakyBucketRateLimiter(max_requests=10, window_seconds=5)
        >>> async def async_api_call():
        ...     return await some_api()

        >>> # Higher burst capacity: 10 req/5sec sustained, 20 burst
        >>> @LeakyBucketRateLimiter(max_requests=10, window_seconds=5, max_burst=20)
        >>> def sync_api_call():
        ...     return requests.get("https://api.example.com")
    """

    def __init__(self, max_requests: int, window_seconds: float, max_burst: Optional[int] = None):
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("requests and window_seconds must be positive")

        if max_burst is not None and max_burst <= 0:
            raise ValueError("max_burst must be positive")

        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_burst = max_burst if max_burst is not None else max_requests

        # Calculate bucket parameters from rate constraints
        self.capacity = float(self.max_burst)  # Burst capacity
        self.leak_rate = max_requests / window_seconds  # Requests per second (sustained rate)
        self._level = 0.0
        self._last_leak_time = time.time()
        self._pending_requests = 0  # Track pending requests to prevent race conditions
        self._lock = asyncio.Lock()

    def __call__(self, func: Callable) -> Callable:
        """Apply rate limiting to a function.

        Args:
            func: The function to be rate limited (sync or async).

        Returns:
            The wrapped function with rate limiting applied.
        """
        return _create_sync_async_wrapper(self._acquire, func)

    async def _acquire(self) -> None:
        """Acquire capacity from the bucket, waiting if necessary."""
        async with self._lock:
            current_time = time.time()

            # Leak tokens based on elapsed time
            time_elapsed = current_time - self._last_leak_time
            leaked_amount = time_elapsed * self.leak_rate
            self._level = max(0.0, self._level - leaked_amount)
            self._last_leak_time = current_time

            # Check if we can proceed immediately
            if self._level + 1.0 <= self.capacity:
                self._level += 1.0
                return

            # Calculate wait time for bucket to leak enough
            # Account for pending requests to ensure proper queuing
            effective_level = self._level + self._pending_requests
            excess = (effective_level + 1.0) - self.capacity
            wait_time = excess / self.leak_rate
            self._pending_requests += 1

        # Wait outside the lock
        if wait_time > 0:
            await asyncio.sleep(wait_time)

        # After waiting, acquire the token
        async with self._lock:
            current_time = time.time()
            time_elapsed = current_time - self._last_leak_time
            leaked_amount = time_elapsed * self.leak_rate
            self._level = max(0.0, self._level - leaked_amount)
            self._last_leak_time = current_time
            self._level += 1.0
            self._pending_requests = max(0, self._pending_requests - 1)

    def status(self) -> Dict[str, float]:
        """Get current bucket status for debugging/monitoring.

        Returns:
            Dictionary containing current bucket state information.
        """
        current_time = time.time()
        time_elapsed = current_time - self._last_leak_time
        leaked_amount = time_elapsed * self.leak_rate
        current_level = max(0.0, self._level - leaked_amount)

        return {
            "level": current_level,
            "capacity": self.capacity,
            "leak_rate": self.leak_rate,
            "utilization": current_level / self.capacity,
            "pending_requests": self._pending_requests,
        }

    def reset(self) -> None:
        """Reset the bucket state."""

        # Use run_async_in_sync instead of asyncio.Runner to avoid deadlocks
        async def _reset():
            async with self._lock:
                self._level = 0.0
                self._last_leak_time = time.time()
                self._pending_requests = 0

        run_async_in_sync(_reset)

    def __enter__(self) -> "LeakyBucketRateLimiter":
        """Enter the sync context manager, applying rate limiting."""
        run_async_in_sync(self._acquire)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the sync context manager."""
        pass

    async def __aenter__(self) -> "LeakyBucketRateLimiter":
        """Enter the async context manager, applying rate limiting."""
        await self._acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the async context manager."""
        pass


class GCRARateLimiter:
    """Generic Cell Rate Algorithm (GCRA) rate limiter for async operations.

    Can be used as both a decorator and async context manager.

    Args:
        max_requests: Maximum number of requests allowed in the time window.
        window_seconds: Time window duration in seconds.

    Raises:
        ValueError: If max_requests or window_seconds are not positive.

    Example:
        >>> @GCRARateLimiter(max_requests=10, window_seconds=2)
        >>> async def async_api_call():
        ...     return await some_api()
    """

    def __init__(self, max_requests: int, window_seconds: float):
        """Initialize the GCRA rate limiter.

        Args:
            max_requests: Maximum number of requests allowed in the time window.
            window_seconds: Time window duration in seconds.
        """
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("requests and window_seconds must be positive")

        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests_per_second = max_requests / window_seconds
        self.increment = 1.0 / self.requests_per_second  # Time between requests (T)
        self.burst_size = max_requests  # Allow full burst capacity
        self.limit = self.burst_size * self.increment  # Maximum bucket level (L)

        # GCRA state
        self._tat = 0.0  # Theoretical Arrival Time
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire permission to make a request.

        Blocks until a token is available according to GCRA.
        """
        async with self._lock:
            now = time.time()

            # GCRA algorithm:
            # TAT' = max(TAT, now) + T
            # If TAT' - now <= L, allow the request and set TAT = TAT'
            # Otherwise, wait until TAT' - L

            new_tat = max(self._tat, now) + self.increment

            if new_tat - now <= self.limit:
                # Request can proceed immediately
                self._tat = new_tat
            else:
                # Need to wait
                wait_time = new_tat - now - self.limit
                await asyncio.sleep(wait_time)
                self._tat = new_tat

    def __call__(self, func: Callable) -> Callable:
        """Apply rate limiting to a function.

        Args:
            func: The function to be rate limited (sync or async).

        Returns:
            The wrapped function with rate limiting applied.

        Usage:
            @limiter
            async def my_async_function():
                # your code here

            @limiter
            def my_sync_function():
                # your code here
        """
        return _create_sync_async_wrapper(self.acquire, func)

    async def __aenter__(self):
        """Async context manager entry.

        Usage:
            async with limiter:
                # your code here
        """
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        # Nothing to clean up
        pass

    def reset(self) -> None:
        """Reset the limiter state (useful for testing)."""
        self._tat = 0.0

    @property
    def estimated_wait_time(self) -> float:
        """Estimate how long the next request would need to wait.

        Note: This is approximate and may change by the time acquire() is called.
        """
        now = time.time()
        new_tat = max(self._tat, now) + self.increment
        return max(0, new_tat - now - self.limit)
