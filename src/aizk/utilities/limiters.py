from abc import ABC, abstractmethod
import asyncio
from functools import partial, wraps
import logging
import time
from typing import (
    Any,
    Callable,
    Optional,
)

from tqdm.auto import tqdm

import tenacity

from aizk.utilities.async_utils import run_async

logger = logging.getLogger(__name__)


def _create_sync_async_wrapper(limiter_method: Callable, original_func: Callable) -> Callable:
    """Create a wrapper that handles both sync and async functions with a limiter (async method)."""
    if asyncio.iscoroutinefunction(original_func):

        @wraps(original_func)
        async def async_wrapper(*args, **kwargs):
            await limiter_method()
            return await original_func(*args, **kwargs)

        return async_wrapper
    else:

        @wraps(original_func)
        def sync_wrapper(*args, **kwargs):
            run_async(limiter_method)
            return original_func(*args, **kwargs)

        return sync_wrapper


class Limiter(ABC):
    """Abstract base class for all limiters."""

    @abstractmethod
    async def acquire(self):
        """Acquire permission to proceed, blocking if necessary according to the limiter's policy."""
        pass

    async def __aenter__(self):
        """Async context manager entry. Calls acquire by default."""
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):  # NOQA: B027
        """Async context manager exit. No-op by default."""
        pass

    def __enter__(self):
        """Sync context manager entry. Calls acquire via run_async by default."""
        run_async(self.acquire)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):  # NOQA: B027
        """Sync context manager exit. No-op by default."""
        pass

    def __call__(self, func: Callable) -> Callable:
        """Wrap a sync or async function with this limiter."""
        return _create_sync_async_wrapper(self.acquire, func)


# --- SlidingWindowRateLimiter ---
class SlidingWindowRateLimiter(Limiter):
    """A sliding time-window rate limiter for both sync and async functions.

    Args:
        max_requests: Maximum number of requests allowed in the time window.
        window_seconds: Time window duration in seconds.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("requests and window_seconds must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._window = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Acquire permission to proceed, blocking if rate limit exceeded."""
        while True:
            async with self._lock:
                now = time.monotonic()
                self._window = [t for t in self._window if t > now - self.window_seconds]
                if len(self._window) < self.max_requests:
                    self._window.append(now)
                    return
                wait_time = self._window[0] + self.window_seconds - now
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            else:
                # Defensive: if wait_time <= 0, loop will retry immediately
                await asyncio.sleep(0)


# --- LeakyBucketRateLimiter ---
class LeakyBucketRateLimiter(Limiter):
    """A leaky bucket rate limiter for both sync and async functions.

    Args:
        max_requests: Maximum number of requests allowed in the time window.
        window_seconds: Time window duration in seconds.
    """

    def __init__(self, max_requests: int, window_seconds: float, capacity: Optional[int] = None):
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("requests and window_seconds must be positive")
        if capacity is not None and capacity <= 0:
            raise ValueError("capacity must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.capacity = float(capacity if capacity is not None else max_requests)
        self.leak_rate = max_requests / window_seconds
        self._level = 0.0
        self._last_leak_time = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Acquire permission to proceed, blocking if bucket is full."""
        while True:
            async with self._lock:
                now = time.monotonic()
                leaked = (now - self._last_leak_time) * self.leak_rate
                self._level = max(0.0, self._level - leaked)
                self._last_leak_time = now
                if self._level + 1.0 <= self.capacity:
                    self._level += 1.0
                    return
                wait_time = ((self._level + 1.0) - self.capacity) / self.leak_rate
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(0)


# --- GCRARateLimiter ---
class GCRARateLimiter(Limiter):
    """Generic Cell Rate Algorithm (GCRA) rate limiter for async operations.

    Args:
        max_requests: Maximum number of requests allowed in the time window.
        window_seconds: Time window duration in seconds.
    """

    def __init__(self, max_requests: int, window_seconds: float):
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
        while True:
            async with self._lock:
                now = time.monotonic()

                # GCRA algorithm:
                # TAT' = max(TAT, now) + T
                # If TAT' - now <= L, allow the request and set TAT = TAT'
                # Otherwise, wait until TAT' - L

                new_tat = max(self._tat, now) + self.increment

                if new_tat - now <= self.limit:
                    # Request can proceed immediately
                    self._tat = new_tat
                    return
                else:
                    # Need to wait
                    wait_time = new_tat - now - self.limit
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(0)

    def reset(self) -> None:
        """Reset the limiter state (useful for testing)."""
        self._tat = 0.0


def rate_limit(limiter: SlidingWindowRateLimiter | LeakyBucketRateLimiter | GCRARateLimiter):
    """Rate limit sync/async functions using a limiter instance (SlidingWindowRateLimiter, LeakyBucketRateLimiter, GCRARateLimiter, etc).

    When stacking with other decorators (e.g., @retry, @concurrency_limit),
    always place @rate_limit closest to the function (innermost), so that
    rate limiting is applied before retries or concurrency limits.

    Examples:
    --------
    >>> limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=1)
    >>> @retry()
    ... @rate_limit(limiter)
    ... async def fetch_data(): ...
    >>> @concurrency_limit(2)
    ... @rate_limit(limiter)
    ... def fetch_sync(): ...
    """

    def decorator(func):
        return limiter(func)

    return decorator


def concurrency_limit(max_concurrent: int):
    """Limit concurrency for async functions.

    When stacking with @rate_limit or @retry, place @concurrency_limit outside @rate_limit
    but inside @retry, so that concurrency is limited for each retry attempt and after rate limiting.

    Examples:
    --------
    >>> limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=1)
    >>> @retry()
    ... @concurrency_limit(2)
    ... @rate_limit(limiter)
    ... async def fetch_data(): ...
    """

    def decorator(func):
        if not asyncio.iscoroutinefunction(func):
            raise ValueError("Function must be async")

        sem = asyncio.Semaphore(max_concurrent)

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            async with sem:
                return await func(*args, **kwargs)

        return async_wrapper

    return decorator


def retry(
    stop: Optional[Any] = None,
    wait: Optional[Any] = None,
    before_sleep: Optional[Callable] = None,
    after: Optional[Callable] = None,
    reraise: bool = True,
    **kwargs,
):
    """Retry sync/async functions using tenacity. Respects rate/concurrency limits if stacked.

    When stacking with @rate_limit or @concurrency_limit, place @retry on the outside
    (outermost), so that retries will respect rate/concurrency limits on each attempt.

    Examples:
    --------
    >>> limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=1)
    >>> @retry()
    ... @rate_limit(limiter)
    ... async def fetch_data(): ...
    >>> @retry(stop=tenacity.stop_after_attempt(5))
    ... @concurrency_limit(2)
    ... def fetch_sync(): ...
    """

    def decorator(func):
        retry_kwargs = dict(
            stop=stop or tenacity.stop_after_attempt(3),
            wait=wait or tenacity.wait_exponential(multiplier=0.5, min=1, max=10),
            reraise=reraise,
            **kwargs,
        )
        if before_sleep is not None:
            retry_kwargs["before_sleep"] = before_sleep
        if after is not None:
            retry_kwargs["after"] = after
        retry_decorator = tenacity.retry(**retry_kwargs)
        return retry_decorator(func)

    return decorator
