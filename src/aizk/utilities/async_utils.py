import asyncio
import logging
import typing as t
from typing import Any, AsyncGenerator, Callable, Coroutine, Iterator, List, Optional, Sequence, Union

from tqdm.auto import tqdm
import uvloop

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logger = logging.getLogger(__name__)


def is_event_loop_running() -> bool:
    """Check if an event loop is currently running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    else:
        return loop.is_running()


def validate_sync_context_for_asyncio() -> None:
    """Validate that we're in a proper synchronous context for asyncio.run().

    Performs comprehensive checks to ensure asyncio.run() or asyncio.gather()
    can be safely executed without conflicts.

    Raises:
        RuntimeError: If called from within an async context or Jupyter/IPython
                      environment where asyncio.run() would cause issues.

    Note:
        This function should be called before using asyncio.run() to ensure
        proper event loop management and avoid nested event loop errors.
    """
    # Check if we're in Jupyter/IPython environment first
    try:
        from IPython.core.getipython import get_ipython

        if get_ipython() is not None:
            logger.warning("Detected Jupyter/IPython environment")
            raise RuntimeError(
                "In Jupyter/IPython, use `await your_async_function()` directly "
                "in a cell; this avoids manual event loop management. "
                "Jupyter automatically provides an async context."
            )
    except ImportError:
        logger.debug("Not in Jupyter/IPython environment")
    else:
        return

    # Check if we're already in a running event loop
    try:
        asyncio.get_running_loop()
        # If we reach here, there's a running loop - this is an error
        logger.error("Attempted to use asyncio.run() from within async context")
    except RuntimeError:
        # No running loop - this is the expected case for asyncio.run()
        logger.debug("Validated sync context - safe to use asyncio.run()")
    else:
        raise RuntimeError(
            "Cannot use asyncio.run() from within an async context. "
            "Use 'await' directly instead of calling async-to-sync helpers."
        )


def run_async_in_sync(
    c: Union[Coroutine[Any, Any, Any], Callable[..., Coroutine[Any, Any, Any]]], *args, **kwargs
) -> Any:
    """Run async code from sync context with proper event loop handling.

    Handles both cases where an event loop is running and where it's not.

    Args:
        c: Either a coroutine or async function to run
        *args: Arguments to pass if c is a function
        **kwargs: Keyword arguments to pass if c is a function

    Returns:
        The result of the async execution

    Note:
        This function should only be called from synchronous code. If you're already
        in an async context, use 'await' directly instead.

    Examples:
        >>> async def fetch_data(url: str) -> str:
        ...     return f"data from {url}"
        >>>
        >>> # From synchronous code:
        >>> result = run_async_in_sync(fetch_data("https://api.example.com"))
        >>>
        >>> # Or pass function with arguments:
        >>> result = run_async_in_sync(fetch_data, "https://api.example.com")
    """
    # Create coroutine if callable was passed
    coro = c(*args, **kwargs) if callable(c) else c

    # Validate we can safely use asyncio.run()
    validate_sync_context_for_asyncio()

    logger.debug("Executing async coroutine with asyncio.run")
    return asyncio.run(coro)


def run_async_tasks(
    tasks: Sequence[Coroutine[Any, Any, Any]],
    show_progress: bool = True,
    progress_bar_desc: str = "Running async tasks",
) -> List[Any]:
    """Execute async tasks concurrently with optional progress tracking.

    Args:
        tasks: Sequence of coroutines to execute concurrently.
        show_progress: Whether to display progress bar during execution.
        progress_bar_desc: Description text for the progress bar.

    Returns:
        List of results from executed tasks in the same order as input tasks.

    Raises:
        RuntimeError: If called from within an async context.
        ValueError: If tasks sequence is empty.

    Examples:
        >>> async def fetch_url(url: str) -> str:
        ...     # ... async implementation
        ...     return data
        >>>
        >>> tasks = [fetch_url(f"https://api.example.com/{i}") for i in range(10)]
        >>> results = run_async_tasks(tasks, show_progress=True)
    """
    if not tasks:
        raise ValueError("Tasks sequence cannot be empty")

    async def _execute_tasks() -> List[Any]:
        """Execute all tasks concurrently with optional progress tracking."""
        if show_progress:
            from tqdm.asyncio import tqdm

            return await tqdm.gather(*tasks, desc=progress_bar_desc)
        else:
            return await asyncio.gather(*tasks)

    # Validate we can safely use asyncio.run()
    validate_sync_context_for_asyncio()

    logger.debug("Executing %d async tasks with asyncio.run", len(tasks))
    return asyncio.run(_execute_tasks())
