"""Shared dotenv loading helpers for conversion process entrypoints."""

from __future__ import annotations

from threading import Lock

from dotenv import load_dotenv

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
