"""Process-level signal dispatcher tests for :mod:`aizk.pipeline.shutdown`.

Covers the design's "two stages share a process" requirement at the signal
boundary: because ``signal.signal`` installs one handler per signal for the
whole process, a single :class:`ShutdownController` cannot own the disposition
when two harnesses share a process. The module-level dispatcher instead
broadcasts every received signal to all registered controllers, so every
harness observes it.

These tests exercise the broadcast and registration semantics directly — they
invoke the dispatcher's handler in-process rather than sending a real OS signal
(which would be racy and could not target a specific in-test handler), and
patch ``signal.signal`` / ``signal.getsignal`` so the real process disposition
is never mutated.
"""

from __future__ import annotations

import signal

import pytest

from aizk.pipeline import shutdown as shutdown_module
from aizk.pipeline.shutdown import ShutdownController


@pytest.fixture
def fake_signal(monkeypatch: pytest.MonkeyPatch) -> dict[int, object]:
    """Patch ``signal.signal`` / ``getsignal`` so installs never touch the process.

    Returns the dict the dispatcher's installed handlers land in, so a test can
    assert which signals were bound without changing the runner's real
    disposition (and without depending on whether the suite runs on the main
    thread).
    """
    installed: dict[int, object] = {}

    def _fake_signal(signum: int, handler: object) -> object:
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    def _fake_getsignal(signum: int) -> object:
        return installed.get(signum, signal.SIG_DFL)

    monkeypatch.setattr(shutdown_module.signal, "signal", _fake_signal)
    monkeypatch.setattr(shutdown_module.signal, "getsignal", _fake_getsignal)
    return installed


@pytest.fixture
def fresh_dispatcher(monkeypatch: pytest.MonkeyPatch) -> shutdown_module._SignalDispatcher:
    """Swap in a clean module-level dispatcher so tests do not share registry state.

    The dispatcher is a process-global singleton; isolating each test behind a
    fresh instance keeps registration assertions independent and leaves the real
    singleton untouched.
    """
    dispatcher = shutdown_module._SignalDispatcher()
    monkeypatch.setattr(shutdown_module, "_dispatcher", dispatcher)
    return dispatcher


def test_one_signal_broadcasts_graceful_to_all_registered_controllers(
    fresh_dispatcher: shutdown_module._SignalDispatcher,
    fake_signal: dict[int, object],
) -> None:
    """A single signal reaches every registered controller, not just one.

    Two controllers register (mirroring two harnesses sharing a process). A
    single simulated SIGTERM — delivered by invoking the dispatcher's installed
    handler directly, never an OS signal — must flip both controllers into
    graceful shutdown, since ``signal.signal`` could only have bound one handler
    for the process.
    """
    first = ShutdownController()
    second = ShutdownController()
    first.install_signal_handlers()
    second.install_signal_handlers()
    assert first.is_shutdown_requested() is False
    assert second.is_shutdown_requested() is False

    # Simulate the OS delivering SIGTERM by invoking the handler the dispatcher
    # installed via signal.signal — no real process signal is sent.
    handler = fake_signal[signal.SIGTERM]
    handler(signal.SIGTERM, None)

    assert first.is_shutdown_requested() is True, "first harness observed the signal"
    assert second.is_shutdown_requested() is True, "second harness also observed the signal"
    assert first.is_immediate_shutdown() is False
    assert second.is_immediate_shutdown() is False


def test_second_signal_broadcasts_immediate_to_all_registered_controllers(
    fresh_dispatcher: shutdown_module._SignalDispatcher,
    fake_signal: dict[int, object],
) -> None:
    """A second signal escalates every registered controller to immediate shutdown."""
    first = ShutdownController()
    second = ShutdownController()
    first.install_signal_handlers()
    second.install_signal_handlers()

    handler = fake_signal[signal.SIGTERM]
    handler(signal.SIGTERM, None)
    handler(signal.SIGINT, None)  # mixed signals both count toward immediate

    assert first.is_immediate_shutdown() is True, "first harness escalated to immediate"
    assert second.is_immediate_shutdown() is True, "second harness escalated to immediate"


def test_dispatcher_installs_process_handlers_once(
    fresh_dispatcher: shutdown_module._SignalDispatcher,
    fake_signal: dict[int, object],
) -> None:
    """Registering a second controller does not reinstall the process handlers.

    The dispatcher owns the single process disposition: it binds SIGTERM/SIGINT
    on the first registration and leaves them in place for subsequent ones, so
    the bound handler stays the dispatcher's broadcasting handler.
    """
    first = ShutdownController()
    first.install_signal_handlers()
    bound_after_first = dict(fake_signal)
    assert set(bound_after_first) == {signal.SIGTERM, signal.SIGINT}, "both signals bound on first install"

    second = ShutdownController()
    second.install_signal_handlers()

    assert fake_signal == bound_after_first, "second registration did not rebind the process handlers"


def test_deregister_last_controller_restores_prior_disposition(
    fresh_dispatcher: shutdown_module._SignalDispatcher,
    fake_signal: dict[int, object],
) -> None:
    """Restoring the last controller returns the process to its prior disposition.

    A deregistered controller stops receiving broadcasts, and once the last one
    leaves, the signal disposition installed at first-register is rolled back so
    a harness's signal install leaves no residue.
    """
    # Establish a pre-existing disposition the dispatcher must restore to.
    prior = object()
    fake_signal[signal.SIGTERM] = prior
    fake_signal[signal.SIGINT] = prior

    first = ShutdownController()
    second = ShutdownController()
    first.install_signal_handlers()
    second.install_signal_handlers()

    # Deregister the first; the second still keeps handlers installed.
    first.restore_signal_handlers()
    handler = fake_signal[signal.SIGTERM]
    handler(signal.SIGTERM, None)
    assert first.is_shutdown_requested() is False, "deregistered controller no longer receives broadcasts"
    assert second.is_shutdown_requested() is True, "still-registered controller receives the broadcast"

    # Deregister the last controller; prior disposition is restored.
    second.restore_signal_handlers()
    assert fake_signal[signal.SIGTERM] is prior, "SIGTERM disposition restored to its prior handler"
    assert fake_signal[signal.SIGINT] is prior, "SIGINT disposition restored to its prior handler"
