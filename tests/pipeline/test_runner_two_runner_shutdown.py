"""Two-runner-in-one-process shutdown test (gates G1 + G5).

Covers the process-global ownership invariant: when two distinct
:class:`StageRunner` instances — each with its own
:class:`ShutdownController`, its own engine, and its own stage — share one
process and both have work in flight, a *single* termination signal must reach
both runners so neither is left running.

This is the integration of the per-runner drain loop with the process-level
signal dispatcher's broadcast. It is distinct from:

- ``test_runner_adapter::test_two_stores_share_runner`` (two stores, no
  shutdown, run sequentially), and
- ``test_runner_shutdown`` (two *controllers* observe one dispatcher signal,
  but no running runner loops / drain).

Here both ``run()`` loops are live with in-flight units, and the simulated
SIGTERM is delivered by invoking the dispatcher's installed handler directly —
never a real OS signal, which would be racy and could not target the in-test
handler. ``signal.signal`` / ``getsignal`` are patched so the runner's real
disposition is never mutated.

Execution is thread-pool (not asyncio); the act phase wraps ``no_thread_leaks``.
"""

from __future__ import annotations

import signal
import threading

from pyleak import no_thread_leaks
import pytest

from aizk.pipeline import shutdown as shutdown_module
from aizk.pipeline.lifecycle import WorkUnitStatus
import aizk.pipeline.runner as runner_module
from aizk.pipeline.runner import StageRunner
from aizk.pipeline.shutdown import ShutdownController

from ._stub_handler import StubStageHandler, create_stub_engine


@pytest.fixture
def fake_signal(monkeypatch: pytest.MonkeyPatch) -> dict[int, object]:
    """Patch ``signal.signal`` / ``getsignal`` so installs never touch the process.

    Returns the dict the dispatcher's installed handlers land in, so the test can
    invoke the captured SIGTERM handler directly without sending a real OS signal
    and without mutating the runner's disposition.
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
    """Swap in a clean module-level dispatcher so the test owns the registry.

    Isolates the broadcast registry from the process-global singleton so the
    test's two controllers are the only ones a simulated signal reaches.
    """
    dispatcher = shutdown_module._SignalDispatcher()
    monkeypatch.setattr(shutdown_module, "_dispatcher", dispatcher)
    return dispatcher


def test_one_signal_drains_two_harnesses_in_one_process(
    fresh_dispatcher: shutdown_module._SignalDispatcher,
    fake_signal: dict[int, object],
) -> None:
    """A single dispatcher signal drains both runners; neither is left running.

    Two runners (distinct stores, stages, and controllers) each run a unit that
    blocks until shutdown is requested, then finishes. Both ``run()`` loops are
    live with work in flight. One simulated SIGTERM — invoked through the
    dispatcher's captured handler — must broadcast to both controllers so both
    loops stop claiming, drain their in-flight unit to a terminal outcome, and
    exit cleanly (code 0), with neither unit left running.
    """
    engine_a = create_stub_engine()
    engine_b = create_stub_engine()
    handler_a = StubStageHandler(engine_a, stage_name="alpha", concurrency_limit=1)
    handler_b = StubStageHandler(engine_b, stage_name="beta", concurrency_limit=1)

    started_a = threading.Event()
    started_b = threading.Event()
    release = threading.Event()

    def _block_until_release_a(_label: str) -> str:
        started_a.set()
        release.wait(timeout=5.0)
        return "ok-a"

    def _block_until_release_b(_label: str) -> str:
        started_b.set()
        release.wait(timeout=5.0)
        return "ok-b"

    unit_a = handler_a.enqueue("a", behavior=_block_until_release_a)
    unit_b = handler_b.enqueue("b", behavior=_block_until_release_b)

    controller_a = ShutdownController()
    controller_b = ShutdownController()
    # Register both controllers from the main thread so the dispatcher installs
    # (and captures) the OS-level handler; runner.run()'s own registration on a
    # background thread is then an idempotent no-op (and would skip the install).
    controller_a.install_signal_handlers()
    controller_b.install_signal_handlers()
    assert signal.SIGTERM in fake_signal, "dispatcher captured the SIGTERM handler on the main thread"

    runner_a = StageRunner(handler_a, engine_a, shutdown=controller_a, poll_interval=0.01, drain_timeout=5.0)
    runner_b = StageRunner(handler_b, engine_b, shutdown=controller_b, poll_interval=0.01, drain_timeout=5.0)

    exit_a: list[int] = []
    exit_b: list[int] = []

    with no_thread_leaks(action="raise"):
        driver_a = threading.Thread(target=lambda: exit_a.append(runner_a.run()))
        driver_b = threading.Thread(target=lambda: exit_b.append(runner_b.run()))
        driver_a.start()
        driver_b.start()
        assert started_a.wait(timeout=5.0), "runner A has a unit in flight"
        assert started_b.wait(timeout=5.0), "runner B has a unit in flight"

        # One simulated SIGTERM via the dispatcher's captured handler — no real signal.
        handler = fake_signal[signal.SIGTERM]
        handler(signal.SIGTERM, None)

        # Both controllers must have observed the single broadcast signal.
        assert controller_a.is_shutdown_requested(), "runner A observed the signal"
        assert controller_b.is_shutdown_requested(), "runner B observed the signal"

        release.set()  # let both in-flight units finish within the drain window
        driver_a.join(timeout=10.0)
        driver_b.join(timeout=10.0)
        assert not driver_a.is_alive(), "runner A exited"
        assert not driver_b.is_alive(), "runner B exited"

    assert exit_a == [0], "runner A drained cleanly"
    assert exit_b == [0], "runner B drained cleanly"
    assert handler_a.get_status(unit_a) == WorkUnitStatus.SUCCEEDED.value, "runner A unit reached a terminal outcome"
    assert handler_b.get_status(unit_b) == WorkUnitStatus.SUCCEEDED.value, "runner B unit reached a terminal outcome"


def test_setproctitle_is_last_write_wins_across_harnesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process title is last-write-wins when two runners share a process.

    Verifies the documented multi-runner ``setproctitle`` semantics (one
    process-global slot, last write wins): two runners each advertise their
    stage role, and the most recently set title is the one that remains. The
    title is an advisory role hint, not a per-runner identifier.
    """
    titles: list[str] = []
    monkeypatch.setattr(runner_module, "setproctitle", lambda title: titles.append(title))

    engine_a = create_stub_engine()
    engine_b = create_stub_engine()
    handler_a = StubStageHandler(engine_a, stage_name="alpha")
    handler_b = StubStageHandler(engine_b, stage_name="beta")
    runner_a = StageRunner(handler_a, engine_a, poll_interval=0.01)
    runner_b = StageRunner(handler_b, engine_b, poll_interval=0.01)

    runner_a.set_process_title()
    runner_b.set_process_title()

    assert titles == ["aizk-stage-alpha", "aizk-stage-beta"], "both runners wrote the shared title slot in order"
    assert titles[-1] == "aizk-stage-beta", "the last write wins — operators see the most recently set stage role"
