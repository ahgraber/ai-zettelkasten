from abc import ABC, abstractmethod
import asyncio
from collections import deque
from functools import wraps
import logging
import time
from typing import (
    Any,
    Awaitable,
    Callable,
    Optional,
    ParamSpec,
    TypeVar,
)

from tqdm.auto import tqdm

import tenacity

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

class Limiter(ABC):
    """Abstract base class for all limiters."""

    @abstractmethod
    async def acquire(self) -> None:
        """Acquire permission to proceed, blocking if necessary according to the limiter's policy."""
        pass

    async def __aenter__(self) -> "Limiter":
        """Async context manager entry. Calls acquire by default."""
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # NOQA: B027
        """Async context manager exit. No-op by default."""
        pass

    def __enter__(self) -> None:
        """Prevent synchronous usage of async-only limiters."""
        raise RuntimeError("Limiter supports only asynchronous context management. Use 'async with'.")

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # NOQA: B027
        """Prevent synchronous usage of async-only limiters."""
        raise RuntimeError("Limiter supports only asynchronous context management. Use 'async with'.")

    def __call__(
        self,
        func: Callable[P, Awaitable[R]],
    ) -> Callable[P, Awaitable[R]]:
        """Wrap an async function with this limiter."""
        if not asyncio.iscoroutinefunction(func):
            raise TypeError("Limiter can only wrap async functions.")

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            await self.acquire()
            return await func(*args, **kwargs)

        return async_wrapper


# --- SlidingWindowRateLimiter ---
class SlidingWindowRateLimiter(Limiter):
    """A sliding time-window rate limiter for both sync and async functions.

    Args:
        max_requests: Maximum number of requests allowed in the time window.
        window_seconds: Time window duration in seconds.

    Note:
        Instances are bound to the first event loop that awaits ``acquire``.
        Avoid sharing a limiter across multiple asyncio event loops or threads.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("requests and window_seconds must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._window = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire permission to proceed, blocking if rate limit exceeded."""
        while True:
            async with self._lock:
                now = time.monotonic()
                window_threshold = now - self.window_seconds
                while self._window and self._window[0] <= window_threshold:
                    self._window.popleft()
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
        max_burst: Optional override for bucket capacity (number of tokens allowed to accumulate).

    Note:
        Instances are bound to the first event loop that awaits ``acquire``.
        Avoid sharing a limiter across multiple asyncio event loops or threads.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        max_burst: Optional[int] = None,
    ) -> None:
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("requests and window_seconds must be positive")
        if max_burst is not None and max_burst <= 0:
            raise ValueError("max_burst must be positive")

        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.capacity = float(max_burst if max_burst is not None else max_requests)
        self.leak_rate = max_requests / window_seconds
        self._level = 0.0
        self._last_leak_time = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
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

    Note:
        Instances are bound to the first event loop that awaits ``acquire``.
        Avoid sharing a limiter across multiple asyncio event loops or threads.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
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


def rate_limit(
    limiter: Limiter,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Rate limit async functions using a limiter instance.

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
    ... async def fetch_data(): ...
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        return limiter(func)

    return decorator


def concurrency_limit(
    max_concurrent: int,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
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
    if max_concurrent <= 0:
        raise ValueError("concurrency must be positive")

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        if not asyncio.iscoroutinefunction(func):
            raise ValueError("Function must be async")

        sem = asyncio.Semaphore(max_concurrent)

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
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
) -> Callable[[Callable[P, R]], Callable[P, R]]:
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

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
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
