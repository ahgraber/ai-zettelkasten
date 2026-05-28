"""Per-instance graceful-shutdown control for a pipeline-stage harness.

Generalizes ``aizk.conversion.workers.shutdown`` from module-global state to a
per-:class:`ShutdownController` instance so two stage harnesses can share one
process without sharing drain bookkeeping. OS signals are inherently
process-wide; the *state* they set (shutdown requested, force-exit requested) is
per-instance. A controller's :meth:`ShutdownController._handle_signal` flips its
own events: the first signal requests a graceful drain, a second requests
immediate termination.

Process-global vs instance-local ownership. ``signal.signal`` installs exactly
one handler per signal for the whole process, so a single controller cannot own
the disposition when two harnesses share a process. The genuinely process-global
piece — the installed SIGTERM/SIGINT disposition and the set of controllers it
must reach — is owned by the module-level :data:`_dispatcher`
(:class:`_SignalDispatcher`). Its handlers **broadcast** every received signal to
*all* registered controllers (first signal → each controller's graceful request;
second → each controller's immediate request), so every harness in the process
observes the signal. Drain *bookkeeping* stays per-:class:`ShutdownController`;
only the signal disposition and registry are module-global.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from types import FrameType

logger = logging.getLogger(__name__)


class _SignalDispatcher:
    """Process-level owner of SIGTERM/SIGINT disposition and the controller registry.

    There is exactly one module-level instance (:data:`_dispatcher`). It exists
    because ``signal.signal`` is one-handler-per-signal for the whole process:
    binding the disposition to a single controller would mean a second harness in
    the same process never observes a signal. Instead the dispatcher installs the
    process handlers **once** and broadcasts each signal to every registered
    :class:`ShutdownController`, so every harness in the process is shut down.

    Registration is thread-safe and idempotent. Installing the actual
    ``signal.signal`` disposition still requires the main thread; a controller
    registered off the main thread is recorded (and will be broadcast to) but the
    OS-level install is skipped with a log — matching ``signal``'s constraint.
    """

    def __init__(self) -> None:
        """Initialize an empty registry with no process handlers installed."""
        self._lock = threading.Lock()
        self._controllers: list[ShutdownController] = []
        self._installed = False
        self._previous_handlers: dict[int, object] = {}

    def register(self, controller: ShutdownController) -> None:
        """Add ``controller`` to the broadcast set and install handlers if possible.

        Idempotent per controller. The first registration on the main thread
        installs the process-wide SIGTERM/SIGINT handlers (recording the prior
        disposition for restoration). Registration off the main thread still adds
        the controller — so it receives broadcasts once handlers are installed —
        but skips the OS-level install with a log, since ``signal.signal`` may
        only be called from the main thread.
        """
        with self._lock:
            if controller not in self._controllers:
                self._controllers.append(controller)
            if self._installed:
                return
            if threading.current_thread() is not threading.main_thread():
                logger.debug("Not on main thread; deferring signal-handler installation")
                return
            for sig in (signal.SIGTERM, signal.SIGINT):
                self._previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
            self._installed = True
            logger.debug("Installed process-wide SIGTERM/SIGINT handlers for graceful shutdown")

    def deregister(self, controller: ShutdownController) -> None:
        """Remove ``controller``; restore the prior disposition when none remain.

        When the last registered controller deregisters, the process-wide
        handlers installed by :meth:`register` are restored to whatever was in
        place beforehand, so a harness's signal install leaves no residue.
        """
        with self._lock:
            try:
                self._controllers.remove(controller)
            except ValueError:
                pass
            if self._controllers or not self._installed:
                return
            for sig, handler in self._previous_handlers.items():
                signal.signal(sig, handler)  # type: ignore[arg-type]
            self._previous_handlers.clear()
            self._installed = False
            logger.debug("Restored process-wide SIGTERM/SIGINT handlers")

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        """Broadcast a received signal to every registered controller.

        First signal → each controller's graceful request; a second → each
        controller's immediate request. Iterates over a snapshot taken under the
        lock so a controller deregistering concurrently cannot break the sweep.
        """
        with self._lock:
            controllers = list(self._controllers)
        for controller in controllers:
            controller._handle_signal(signum, frame)


_dispatcher = _SignalDispatcher()
"""The single process-level signal dispatcher (process-global by design)."""


class ShutdownController:
    """Per-instance shutdown state plus optional process-wide signal handlers.

    Drain bookkeeping (``is_shutdown_requested`` / ``is_immediate_shutdown``) is
    held on the instance so multiple harnesses in one process keep independent
    state. :meth:`install_signal_handlers` is optional — a harness driven
    programmatically (tests, embedding) can call :meth:`request_shutdown`
    directly without touching process signal disposition.

    Signal disposition is process-global, so it is owned by the module-level
    :data:`_dispatcher`, not by any single controller: installing handlers
    *registers* this controller with the dispatcher, which broadcasts every
    signal to all registered controllers. This lets two harnesses sharing a
    process both observe the same signal — which a per-instance
    ``signal.signal`` install (one-handler-per-signal) could not provide.
    """

    def __init__(self) -> None:
        """Initialize a controller with cleared shutdown state."""
        self._shutdown_event = threading.Event()
        self._signal_count = 0
        self._signal_lock = threading.Lock()

    def is_shutdown_requested(self) -> bool:
        """Return ``True`` once a graceful shutdown has been requested."""
        return self._shutdown_event.is_set()

    def is_immediate_shutdown(self) -> bool:
        """Return ``True`` once a second signal has requested immediate termination."""
        with self._signal_lock:
            return self._signal_count >= 2

    def request_shutdown(self) -> None:
        """Programmatically request a graceful shutdown (no signal required)."""
        self._shutdown_event.set()

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        """Set shutdown state on SIGTERM/SIGINT.

        First signal requests a graceful drain; a second requests immediate
        (forceful) termination.
        """
        sig_name = signal.Signals(signum).name
        with self._signal_lock:
            self._signal_count += 1
            count = self._signal_count
        if count == 1:
            logger.info("Received %s — initiating graceful shutdown", sig_name)
            self._shutdown_event.set()
        else:
            logger.warning("Received %s again — forcing immediate shutdown", sig_name)

    def install_signal_handlers(self) -> None:
        """Register this controller with the process-level signal dispatcher.

        Process signal disposition is one-handler-per-signal, so it is owned by
        the module-level :data:`_dispatcher`, which broadcasts each signal to
        every registered controller — letting two harnesses in one process both
        observe a signal. Registration is idempotent. The dispatcher installs
        the OS-level ``signal.signal`` handlers once and only from the main
        thread; an off-main-thread harness (embedding, test driver) is still
        registered for broadcast but the OS-level install is skipped with a log,
        so the harness still runs and can be shut down via
        :meth:`request_shutdown`.
        """
        _dispatcher.register(self)

    def restore_signal_handlers(self) -> None:
        """Deregister this controller from the process-level signal dispatcher.

        When the last registered controller deregisters, the dispatcher restores
        whatever signal disposition was in place before the first install.
        """
        _dispatcher.deregister(self)


def force_exit(code: int = 1) -> None:
    """Exit the process immediately via ``os._exit``, bypassing atexit handlers.

    Seam for tests: a patchable wrapper around ``os._exit``. ``os._exit``
    bypasses non-daemon ``ThreadPoolExecutor`` worker joins that would otherwise
    keep a stuck task alive during interpreter shutdown.

    Multi-harness semantics: ``os._exit`` terminates the **whole process**, not a
    single harness. When two harnesses share a process, one harness's forced exit
    tears down both (and every other thread/pool in the process). This is a
    deliberate last-resort escape for a stuck/uncooperative in-process unit; a
    cooperative per-harness shutdown drains via :class:`ShutdownController`
    instead.
    """
    os._exit(code)
