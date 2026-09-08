# ruff: NOQA: E731
import contextlib
import os
import time
from typing import (
    Any,
    Callable,
    Optional,
)

from tqdm.auto import tqdm

import tenacity

#: Interval between liveness polls while waiting for a signalled process to exit.
_POLL_INTERVAL_SECONDS = 0.05


# %%
@contextlib.contextmanager
def temp_env_var(key, value):
    """Context manager to temporarily set an environment variable."""
    original_value = os.getenv(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if original_value is not None:
            os.environ[key] = original_value
        else:
            del os.environ[key]


def _wait_until_exited(is_alive: Callable[[], bool], grace_seconds: float) -> bool:
    """Poll ``is_alive`` until it reports false or ``grace_seconds`` elapse.

    Args:
        is_alive: Predicate that is true while the process is still running.
        grace_seconds: Longest time to keep polling.

    Returns:
        ``True`` if the process exited within the grace period.
    """
    deadline = time.monotonic() + grace_seconds
    while is_alive():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_SECONDS)
    return True


def terminate_process_group(
    terminate: Callable[[], None],
    kill: Callable[[], None],
    is_alive: Callable[[], bool],
    *,
    grace_seconds: float = 5,
    wait_after_kill: bool = True,
) -> None:
    """Stop a process group: request termination, then force it if it does not exit.

    Escalation is decided by liveness rather than by how a particular wait call
    reports a timeout, so callers over different process families (a ``Popen``
    process group, a ``multiprocessing.Process``) share one sequence. The
    callables carry every caller-specific concern — how a signal is delivered,
    which process-group guard applies, and what is logged — because those differ
    per site while the sequence does not.

    Args:
        terminate: Requests a graceful stop (typically ``SIGTERM``).
        kill: Forces a stop (typically ``SIGKILL``); called only when the process
            is still alive after the grace period.
        is_alive: Predicate that is true while the process is still running.
        grace_seconds: Time allowed after each signal before giving up on it.
        wait_after_kill: Whether to keep polling after ``kill``, so the call
            returns only once the process is gone or the grace period expires.
    """
    terminate()
    if _wait_until_exited(is_alive, grace_seconds):
        return
    kill()
    if wait_after_kill:
        _wait_until_exited(is_alive, grace_seconds)
