import asyncio
from functools import partial, wraps
import logging
import sys
from typing import (
    Any,
    Callable,
    Coroutine,
    TypeVar,
    Union,
)

try:
    import uvloop  # type: ignore

    if sys.platform != "win32":
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    uvloop = None

logger = logging.getLogger(__name__)
T = TypeVar("T")


def is_event_loop_running() -> bool:
    """Check if an event loop is currently running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def run_async(
    coro_or_func: Union[Coroutine[Any, Any, T], Callable[..., Coroutine[Any, Any, T]]], *args, **kwargs
) -> T:
    """Run an async coroutine from sync code, handling event loop state."""
    # Check if we're in a Jupyter/IPython environment first
    try:
        from IPython.core.getipython import get_ipython

        if get_ipython() is not None:
            logger.warning(
                "run_async is not recommended in Jupyter/IPython. "
                "Use 'await' directly on async functions in notebook cells."
            )
    except ImportError:
        # Not in a Jupyter/IPython environment, which is fine.
        pass

    coro = coro_or_func(*args, **kwargs) if callable(coro_or_func) else coro_or_func
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop, run in a new one.
        return asyncio.run(coro)
    else:
        # A loop is running, submit the coroutine and wait for it from the current thread.
        # This is for sync code called from an async context.
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()
