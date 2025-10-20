"""Async utility helpers for orchestration and cross-environment ergonomics."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
import logging
import sys
from typing import Any, Coroutine, TypeVar, Union

try:
    import uvloop  # type: ignore

    if sys.platform != "win32":
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    uvloop = None

logger = logging.getLogger(__name__)
T = TypeVar("T")
U = TypeVar("U")


def is_event_loop_running() -> bool:
    """Check whether an event loop is active in the current thread.

    Returns:
        bool: ``True`` when an event loop is running, ``False`` otherwise.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


async def tqdm_gather(*aws, return_exceptions: bool = False, **kwargs):
    """Gather awaitables with a tqdm progress bar.

    Args:
        aws: Awaitables to execute concurrently.
        return_exceptions: Whether to return exceptions instead of raising them.
        **kwargs: Additional keyword arguments forwarded to ``tqdm_asyncio.gather``.

    Returns:
        list[Any]: Results or exceptions corresponding to ``aws``.

    Notes:
        Workaround for https://github.com/tqdm/tqdm/issues/1286.
    """
    from tqdm.asyncio import tqdm_asyncio

    if not return_exceptions:
        return await tqdm_asyncio.gather(*aws, **kwargs)

    async def wrap(f):
        try:
            return await f
        except Exception as e:
            return e

    return await tqdm_asyncio.gather(*map(wrap, aws), **kwargs)


def run_async(
    coro_or_func: Union[Coroutine[Any, Any, T], Callable[..., Coroutine[Any, Any, T]]],
    *args,
    **kwargs,
) -> T:
    """Execute a coroutine from synchronous code.

    Args:
        coro_or_func: Coroutine object or callable returning one.
        *args: Positional arguments forwarded to ``coro_or_func`` when callable.
        **kwargs: Keyword arguments forwarded to ``coro_or_func`` when callable.

    Returns:
        T: Result produced by the coroutine.

    Raises:
        RuntimeError: If called while an event loop is already running.
    """
    coro = coro_or_func(*args, **kwargs) if callable(coro_or_func) else coro_or_func
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    coro.close()
    raise RuntimeError("run_async cannot be used while an event loop is running; use 'await' instead.")


async def gather_with_concurrency(
    coroutines: Iterable[Awaitable[T]],
    concurrency: int,
    *,
    return_exceptions: bool = False,
) -> list[Union[T, BaseException]]:
    """Execute awaitables with bounded concurrency while preserving order.

    Args:
        coroutines: Awaitables to execute.
        concurrency: Maximum number of tasks to run simultaneously.
        return_exceptions: Whether to return exceptions instead of raising them.

    Returns:
        list[Union[T, BaseException]]: Results aligned with ``coroutines``.

    Raises:
        ValueError: If ``concurrency`` is non-positive.
    """
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")

    awaitables = list(coroutines)
    if not awaitables:
        return []

    semaphore = asyncio.Semaphore(concurrency)
    results: list[Union[T, BaseException]] = [None] * len(awaitables)  # type: ignore[assignment]

    async def runner(index: int, awaitable: Awaitable[T]) -> None:
        async with semaphore:
            if return_exceptions:
                try:
                    results[index] = await awaitable
                except Exception as exc:  # pragma: no cover - caller controls branch
                    results[index] = exc
                return

            results[index] = await awaitable

    async with asyncio.TaskGroup() as tg:
        for idx, awaitable in enumerate(awaitables):
            tg.create_task(runner(idx, awaitable))

    return results


async def map_concurrently(
    items: Iterable[U],
    func: Callable[[U], Awaitable[T]],
    concurrency: int,
    *,
    return_exceptions: bool = False,
) -> list[Union[T, BaseException]]:
    """Apply an async function to items with bounded concurrency.

    Args:
        items: Inputs to process.
        func: Async callable applied to each input.
        concurrency: Maximum number of in-flight tasks.
        return_exceptions: Whether to return exceptions instead of raising them.

    Returns:
        list[Union[T, BaseException]]: Results in the same order as ``items``.
    """
    coroutines = (func(item) for item in items)
    return await gather_with_concurrency(coroutines, concurrency, return_exceptions=return_exceptions)
