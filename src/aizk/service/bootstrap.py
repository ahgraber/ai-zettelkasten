"""Process-level bootstrap steps every aizk entrypoint performs.

The two steps here are the ones a process takes before it reads any settings:
it makes ``.env`` part of the process environment, at most once, and it gives
itself a descriptive title so a host running several aizk processes can tell
them apart.
"""

from __future__ import annotations

from threading import Lock

from dotenv import load_dotenv
from setproctitle import setproctitle

_DOTENV_LOADED = False
_DOTENV_LOCK = Lock()


def load_process_dotenv_once() -> None:
    """Load `.env` at most once in the current process."""
    global _DOTENV_LOADED
    with _DOTENV_LOCK:
        if _DOTENV_LOADED:
            return
        load_dotenv()
        _DOTENV_LOADED = True


def set_process_title(role: str) -> None:
    """Name this process after the role it is running.

    Args:
        role: The title the process reports to the host, for example
            ``"docling-worker"``.
    """
    setproctitle(role)
