import asyncio
from typing import Any, Callable, Coroutine

import uvloop

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


# def is_event_loop_running() -> bool:
#     """Check if an event loop is currently running."""
#     try:
#         loop = asyncio.get_running_loop()
#     except RuntimeError:
#         return False
#     else:
#         return loop.is_running()


# def is_nest_asyncio_applied() -> bool:
#     """Check if nest_asyncio is applied."""
#     if not is_event_loop_running():
#         return False

#     try:
#         # If nest_asyncio is applied, this should work in a running loop
#         async def dummy():
#             return True

#         _ = asyncio.run(dummy())
#     except RuntimeError:
#         return False
#     else:
#         return True


# def get_create_event_loop() -> asyncio.AbstractEventLoop:
#     """Get or create a running event loop."""
#     # NOTE: nest_asyncio is deprecated
#     # try:
#     #     from IPython.core.getipython import get_ipython

#     #     if get_ipython() is not None:
#     #         import nest_asyncio

#     #         nest_asyncio.apply()

#     # except ImportError:
#     #     pass

#     try:
#         loop = asyncio.get_running_loop()
#     except RuntimeError:
#         loop = asyncio.new_event_loop()

#     return loop


def synchronize(afunc: Callable[..., Coroutine[Any, Any, Any]], *args, **kwargs):
    """Run async function in synchronous context."""
    try:
        # Check if we're already in a running event loop
        loop = asyncio.get_running_loop()
        # If we get here, we're in a running loop - need nest_asyncio
        try:
            import nest_asyncio
        except ImportError as e:
            raise ImportError(
                "It seems like you're running this in a jupyter-like environment. "
                "Please install nest_asyncio with `pip install nest_asyncio` to make it work."
            ) from e

        nest_asyncio.apply()
        # Call the async function with args/kwargs to create the coroutine
        coro = afunc(*args, **kwargs)
        return loop.run_until_complete(coro)

    except RuntimeError:
        # No running event loop, so we can use asyncio.run
        coro = afunc(*args, **kwargs)
        return asyncio.run(coro)
