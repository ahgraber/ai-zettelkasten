"""Graceful shutdown for the conversion worker process.

Provides signal handling and drain logic so the worker can finish in-flight
jobs before exiting on SIGTERM or SIGINT.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

logger = logging.getLogger(__name__)

# Module-level shutdown state.  One instance per worker process.
_shutdown_event = threading.Event()
_signal_count = 0
_signal_count_lock = threading.Lock()


def is_shutdown_requested() -> bool:
    """Return True if a shutdown signal has been received."""
    return _shutdown_event.is_set()


def is_immediate_shutdown() -> bool:
    """Return True if a second signal requests immediate termination."""
    with _signal_count_lock:
        return _signal_count >= 2


def request_shutdown() -> None:
    """Programmatically request a shutdown (for testing)."""
    _shutdown_event.set()


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    """Handle SIGTERM/SIGINT by setting the shutdown event.

    First signal: graceful drain.  Second signal: immediate termination.
    """
    global _signal_count  # noqa: PLW0603
    sig_name = signal.Signals(signum).name

    with _signal_count_lock:
        _signal_count += 1
        count = _signal_count

    if count == 1:
        logger.info("Received %s — initiating graceful shutdown", sig_name)
        _shutdown_event.set()
    else:
        logger.warning("Received %s again — forcing immediate shutdown", sig_name)


def register_signal_handlers() -> None:
    """Install SIGTERM and SIGINT handlers for graceful shutdown.

    Must be called from the main thread.
    """
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    logger.debug("Registered SIGTERM/SIGINT handlers for graceful shutdown")


def reset() -> None:
    """Reset shutdown state.  Intended for tests only."""
    global _signal_count  # noqa: PLW0603
    _shutdown_event.clear()
    with _signal_count_lock:
        _signal_count = 0
