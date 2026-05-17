"""Subprocess supervision for conversion jobs."""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
import logging
import multiprocessing as mp
import os
import queue as queue_module
import signal
import time

from aizk.conversion.workers.types import SupervisionResult

logger = logging.getLogger(__name__)

# Signature: (phase_name, reported_at) -> None. Called once per phase event
# drained from the subprocess status queue. The orchestrator wires this to
# `record_phase_event` so each phase report becomes a row in the event log.
PhaseEventCallback = Callable[[str, dt.datetime], None]


def _get_parent_pgid() -> int | None:
    """Return the parent process group id, if available."""
    try:
        return os.getpgrp()
    except OSError:
        return None


def _terminate_child_process(process: mp.Process, parent_pgid: int | None, sig: int) -> None:
    """Terminate the child process or its process group safely."""
    if not process.pid:
        return
    try:
        pgid = os.getpgid(process.pid)
        if parent_pgid is not None and pgid == parent_pgid:
            os.kill(process.pid, sig)
        else:
            os.killpg(pgid, sig)
    except (ProcessLookupError, OSError):
        return


def _collect_status_messages(
    *,
    job_id: int,
    status_queue: mp.Queue,
    last_phase: str,
    reported_error: dict[str, str] | None,
    on_phase_event: PhaseEventCallback | None = None,
) -> tuple[str, dict[str, str] | None]:
    """Drain the status queue, updating phase and error state.

    When ``on_phase_event`` is supplied, it is invoked once for every phase
    report drained (regardless of whether the reported phase changed from
    ``last_phase``), so each subprocess phase report becomes its own event-log
    row. The callback receives ``(phase, reported_at)`` and is expected to
    record best-effort — exceptions raised by it are logged and swallowed so
    supervision continues.
    """
    try:
        while True:
            message = status_queue.get_nowait()
            event = message.get("event")
            if event == "phase":
                new_phase = message.get("message", last_phase)
                if on_phase_event is not None:
                    try:
                        on_phase_event(new_phase, dt.datetime.now(dt.timezone.utc))
                    except Exception:
                        logger.warning(
                            "Phase-event callback raised for job %s phase %r",
                            job_id,
                            new_phase,
                            exc_info=True,
                        )
                if new_phase != last_phase:
                    last_phase = new_phase
                    logger.info("Job %s entered phase %s", job_id, last_phase)
            elif event == "failed":
                reported_error = message
    except queue_module.Empty:
        pass
    return last_phase, reported_error


def _terminate_and_wait(
    process: mp.Process,
    parent_pgid: int | None,
) -> None:
    """SIGTERM → wait 5s → SIGKILL → wait 5s."""
    _terminate_child_process(process, parent_pgid, signal.SIGTERM)
    process.join(timeout=5.0)
    if process.is_alive() and process.pid:
        _terminate_child_process(process, parent_pgid, signal.SIGKILL)
        process.join(timeout=5.0)


def _supervise_conversion_process(
    *,
    job_id: int,
    process: mp.Process,
    status_queue: mp.Queue,
    poll_interval_seconds: float,
    deadline: float | None,
    timeout_seconds: float,
    is_cancelled_fn: Callable[[], bool],
    shutdown_requested_fn: Callable[[], bool] | None = None,
    drain_timeout_seconds: float = 300.0,
    on_phase_event: PhaseEventCallback | None = None,
) -> SupervisionResult:
    """Monitor the subprocess for cancellation, timeout, or shutdown.

    Returns a ``SupervisionResult`` describing how the subprocess ended.
    The caller is responsible for acting on ``timed_out``, ``cancelled``,
    or ``shutdown_terminated``.
    """
    last_phase = "starting"
    reported_error: dict[str, str] | None = None
    parent_pgid = _get_parent_pgid()
    drain_deadline: float | None = None

    while process.is_alive():
        last_phase, reported_error = _collect_status_messages(
            job_id=job_id,
            status_queue=status_queue,
            last_phase=last_phase,
            reported_error=reported_error,
            on_phase_event=on_phase_event,
        )

        if is_cancelled_fn():
            _terminate_and_wait(process, parent_pgid)
            logger.info("Job %s cancelled during %s", job_id, last_phase)
            return SupervisionResult(last_phase, reported_error, True, False)

        if deadline and time.monotonic() >= deadline:
            _terminate_and_wait(process, parent_pgid)
            elapsed = time.monotonic() - (deadline - timeout_seconds)
            logger.info(
                "Job %s timed out during %s after %s seconds",
                job_id,
                last_phase,
                round(elapsed, 3),
            )
            return SupervisionResult(last_phase, reported_error, False, True)

        # Shutdown drain: on first detection, set a drain deadline.
        if shutdown_requested_fn is not None and shutdown_requested_fn():
            if drain_deadline is None:
                drain_deadline = time.monotonic() + drain_timeout_seconds
                logger.info(
                    "Shutdown requested — draining job %s (timeout=%ds)",
                    job_id,
                    drain_timeout_seconds,
                )
            if time.monotonic() >= drain_deadline:
                _terminate_and_wait(process, parent_pgid)
                logger.warning(
                    "Job %s force-terminated during %s after drain timeout (%ds)",
                    job_id,
                    last_phase,
                    drain_timeout_seconds,
                )
                return SupervisionResult(
                    last_phase,
                    reported_error,
                    False,
                    False,
                    shutdown_terminated=True,
                )

        process.join(timeout=poll_interval_seconds)

    last_phase, reported_error = _collect_status_messages(
        job_id=job_id,
        status_queue=status_queue,
        last_phase=last_phase,
        reported_error=reported_error,
    )
    return SupervisionResult(last_phase, reported_error, False, False)
