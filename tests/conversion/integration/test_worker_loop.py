"""Integration tests for shutdown-aware subprocess supervision.

The engine-loop concerns this file used to cover — ``run_worker``'s graceful
shutdown, the immediate-shutdown ``force_exit`` seam, and the
``_drain_in_flight`` drain loop — are now owned by the pipeline runner and
covered by ``tests/pipeline/test_runner_shutdown.py`` (signal dispatch),
``tests/pipeline/test_runner_drain.py`` (drain + forced exit), and
``tests/conversion/integration/test_worker_e2e.py`` (real-subprocess
drain on shutdown).

What remains here is conversion-specific: the ``shutdown_requested_fn`` drain
branch inside :func:`~aizk.conversion.workers.supervision._supervise_conversion_process`.
That seam is still live — the runner owns drain via the ``terminate_event`` seam
and passes ``None`` — but the branch itself remains and these tests pin its
behavior using a plain local flag.
"""

from __future__ import annotations

import os
import queue
import signal
import time

from aizk.conversion.workers.supervision import _supervise_conversion_process


class _StubProcess:
    """Process stub that stays alive for a number of join cycles.

    Unlike a no-op stub, ``join`` actually sleeps for the requested timeout
    so that ``time.monotonic()`` advances naturally.  This lets tests use
    real (short) deadlines instead of mocking the clock.
    """

    def __init__(self, alive_cycles: int = 3) -> None:
        self._alive_cycles = alive_cycles
        self._alive = True
        self.pid = 99999
        self.exitcode = 0

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout: float | None = None) -> None:
        if timeout is not None and self._alive:
            time.sleep(timeout)
        if self._alive_cycles > 0:
            self._alive_cycles -= 1
        if self._alive_cycles == 0:
            self._alive = False

    def terminate(self) -> None:
        self._alive = False

    def kill(self) -> None:
        self._alive = False


class TestSupervisionShutdownDrain:
    """Tests for shutdown-aware subprocess supervision.

    The ``shutdown_requested_fn`` is supplied as a plain callable over a local
    flag — the same shape any caller (e.g. a controller) would pass — so the
    tests exercise the supervision loop's drain branch without depending on any
    particular shutdown owner.
    """

    def test_shutdown_during_supervision_job_completes_within_drain(self, monkeypatch):
        """When shutdown is requested but the job finishes before drain timeout, no forced termination."""
        # Process completes after 2 join cycles (~10ms with 5ms poll interval)
        process = _StubProcess(alive_cycles=2)
        status_queue = queue.Queue()

        monkeypatch.setattr(os, "getpgrp", lambda: 111)

        result = _supervise_conversion_process(
            job_id=1,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=0.005,
            deadline=None,
            timeout_seconds=0,
            is_cancelled_fn=lambda: False,
            shutdown_requested_fn=lambda: True,
            drain_timeout_seconds=5.0,  # Long drain — job finishes first
        )

        assert result.shutdown_terminated is False
        assert result.timed_out is False
        assert result.cancelled is False

    def test_drain_timeout_force_terminates_subprocess(self, monkeypatch):
        """When drain timeout expires, subprocess is force-terminated."""
        process = _StubProcess(alive_cycles=100)  # Stays alive long enough
        status_queue = queue.Queue()

        killpg_calls = []
        monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
        monkeypatch.setattr(os, "getpgrp", lambda: 111)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

        # poll_interval=0.005s, drain_timeout=0.01s → drain expires after ~2 join cycles
        result = _supervise_conversion_process(
            job_id=1,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=0.005,
            deadline=None,
            timeout_seconds=0,
            is_cancelled_fn=lambda: False,
            shutdown_requested_fn=lambda: True,
            drain_timeout_seconds=0.01,
        )

        assert result.shutdown_terminated is True
        assert result.timed_out is False
        assert len(killpg_calls) >= 1
        assert killpg_calls[0][1] == signal.SIGTERM

    def test_no_shutdown_fn_means_no_drain(self):
        """Without shutdown_requested_fn, supervision runs normally (the runner path).

        The runner passes ``shutdown_requested_fn=None`` and owns drain via the
        ``terminate_event`` seam; the module-global shutdown-drain branch must be
        dead in that case.
        """
        process = _StubProcess(alive_cycles=1)
        status_queue = queue.Queue()

        result = _supervise_conversion_process(
            job_id=1,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=0.005,
            deadline=None,
            timeout_seconds=0,
            is_cancelled_fn=lambda: False,
            shutdown_requested_fn=None,
        )

        assert result.shutdown_terminated is False

    def test_phase_callback_runs_for_messages_drained_after_process_exit(self):
        """Phase reports left in the queue at subprocess exit are still event-log candidates."""

        class _ExitedProcess:
            pid = 99999
            exitcode = 0

            def is_alive(self) -> bool:
                return False

        process = _ExitedProcess()
        status_queue = queue.Queue()
        status_queue.put_nowait({"event": "phase", "message": "converting"})
        recorded_phases = []

        result = _supervise_conversion_process(
            job_id=1,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=0.005,
            deadline=None,
            timeout_seconds=0,
            is_cancelled_fn=lambda: False,
            on_phase_event=lambda phase, _reported_at: recorded_phases.append(phase),
        )

        assert result.last_phase == "converting"
        assert recorded_phases == ["converting"]

    def test_job_timeout_takes_precedence_over_drain(self, monkeypatch):
        """Job timeout fires before drain timeout."""
        process = _StubProcess(alive_cycles=100)
        status_queue = queue.Queue()

        killpg_calls = []
        monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
        monkeypatch.setattr(os, "getpgrp", lambda: 111)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

        # Job deadline already expired; drain timeout is long.
        # The job timeout check runs before the drain check in the loop,
        # so it should fire first.
        result = _supervise_conversion_process(
            job_id=1,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=0.005,
            deadline=time.monotonic() - 1,  # Already expired
            timeout_seconds=1.0,
            is_cancelled_fn=lambda: False,
            shutdown_requested_fn=lambda: True,
            drain_timeout_seconds=300.0,
        )

        assert result.timed_out is True
        assert result.shutdown_terminated is False
