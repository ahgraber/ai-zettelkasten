"""Tests for the shared process-group termination sequence."""

from __future__ import annotations

from pyleak import no_thread_leaks

from aizk.utilities.process import terminate_process_group

#: Short enough that the escalation path costs milliseconds, long enough that a
#: single poll interval fits inside it.
_GRACE_SECONDS = 0.2


class _FakeProcess:
    """A process that exits after a chosen number of signals, recording each one."""

    def __init__(self, *, exits_on_terminate: bool) -> None:
        """Record whether the fake cooperates with the graceful stop."""
        self.signals: list[str] = []
        self.polls_after_kill = 0
        self._exits_on_terminate = exits_on_terminate
        self._alive = True

    def terminate(self) -> None:
        """Record the graceful stop and exit if this process cooperates."""
        self.signals.append("terminate")
        if self._exits_on_terminate:
            self._alive = False

    def kill(self) -> None:
        """Record the forced stop; the process exits on the next poll."""
        self.signals.append("kill")

    def is_alive(self) -> bool:
        """Report liveness, exiting one poll after the forced stop."""
        if "kill" in self.signals:
            self.polls_after_kill += 1
            self._alive = False
        return self._alive


def _terminate(process: _FakeProcess, *, wait_after_kill: bool = True) -> None:
    """Run the termination sequence over ``process`` with a short grace period."""
    terminate_process_group(
        process.terminate,
        process.kill,
        process.is_alive,
        grace_seconds=_GRACE_SECONDS,
        wait_after_kill=wait_after_kill,
    )


def test_graceful_exit_does_not_escalate() -> None:
    """A process that exits on the graceful stop is never forced."""
    process = _FakeProcess(exits_on_terminate=True)

    with no_thread_leaks(action="raise"):
        _terminate(process)

    assert process.signals == ["terminate"]


def test_unresponsive_process_is_killed() -> None:
    """A process still alive after the grace period is forced."""
    process = _FakeProcess(exits_on_terminate=False)

    with no_thread_leaks(action="raise"):
        _terminate(process)

    assert process.signals == ["terminate", "kill"]


def test_post_kill_wait_polls_until_the_process_is_gone() -> None:
    """With the post-kill wait the call returns only after liveness reports false."""
    process = _FakeProcess(exits_on_terminate=False)

    with no_thread_leaks(action="raise"):
        _terminate(process)

    assert process.polls_after_kill >= 1


def test_post_kill_wait_can_be_skipped() -> None:
    """Disabling the post-kill wait returns immediately after the forced stop."""
    process = _FakeProcess(exits_on_terminate=False)

    with no_thread_leaks(action="raise"):
        _terminate(process, wait_after_kill=False)

    assert process.signals == ["terminate", "kill"]
    assert process.polls_after_kill == 0
