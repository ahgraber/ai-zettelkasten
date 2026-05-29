"""Runner durable-scheduling-state tests across a process restart (gate G3).

Covers the runtime-boundary invariant that every scheduling decision reads
durable, non-lossy DB state — retryability and retry-wait — rather than
in-memory state that a process restart would discard. The runner's eligibility
is delegated entirely to the handler's claim query over the store, so a fresh
handler + runner reconstructed over the *same* database must make the same
scheduling decisions a never-restarted process would.

The setup persists two terminal outcomes through a first runner over a
file-based SQLite DB — a permanent failure and a retryable failure with a future
retry-wait — then disposes that engine (simulating a process exit) and builds a
fresh handler + runner over the same file. The reconstructed runner must NOT
re-claim the permanent failure and must NOT find the retryable failure eligible
until its retry-wait elapses.

Execution is thread-pool (not asyncio); act phases wrap ``no_thread_leaks``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pyleak import no_thread_leaks

from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.runner import StageRunner

from ._stub_handler import StubStageHandler, create_stub_file_engine, succeed


class _PermanentError(RuntimeError):
    """A failure the stub maps to a permanent (non-retryable) terminal outcome."""

    retryable = False


class _RetryableError(RuntimeError):
    """A failure the stub maps to a retryable terminal outcome (sets a retry-wait)."""

    retryable = True


def _permfail(_label: str) -> str:
    raise _PermanentError("permanent failure")


def _retryfail(_label: str) -> str:
    raise _RetryableError("retryable failure")


def test_durable_state_drives_scheduling_after_restart(tmp_path: Path) -> None:
    """A reconstructed runner reads durable state: no permanent retry, retry-wait honored.

    First "process": run a permanent failure and a retryable failure to their
    terminal states, persisting ``retry_class`` and a future
    ``earliest_next_attempt_at`` for the retryable one. Dispose the engine
    (simulated restart). Second "process": a fresh engine + handler + runner
    over the same file must claim neither — the permanent failure is excluded by
    retry class, the retryable one is gated by its still-future retry-wait — so
    neither unit is executed again.
    """
    db_path = str(tmp_path / "stub-restart.db")

    # --- first process: drive both units to durable terminal state ---
    engine_a = create_stub_file_engine(db_path, create_schema=True)
    handler_a = StubStageHandler(engine_a, concurrency_limit=2)
    perm_id = handler_a.enqueue("perm", behavior=_permfail)
    retry_id = handler_a.enqueue("retry", behavior=_retryfail)

    runner_a = StageRunner(handler_a, engine_a, poll_interval=0.01)
    with no_thread_leaks(action="raise"):
        runner_a.run_until_idle()

    assert handler_a.get_status(perm_id) == WorkUnitStatus.FAILED.value, "permanent failure recorded"
    assert handler_a.get_status(retry_id) == WorkUnitStatus.FAILED.value, "retryable failure recorded"
    engine_a.dispose()  # simulate the process exiting; in-memory runner state is gone

    # --- second process: fresh engine + handler + runner over the same file ---
    engine_b = create_stub_file_engine(db_path, create_schema=False)
    handler_b = StubStageHandler(engine_b, concurrency_limit=2)
    runner_b = StageRunner(handler_b, engine_b, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner_b.run_until_idle()

    assert handler_b.recorded.execute_started == [], "restarted runner re-executed nothing ineligible"
    assert handler_b.get_status(perm_id) == WorkUnitStatus.FAILED.value, (
        "permanent failure not re-claimed after restart"
    )
    assert handler_b.get_status(retry_id) == WorkUnitStatus.FAILED.value, (
        "retryable failure still gated by its persisted retry-wait after restart"
    )
    engine_b.dispose()


def test_durable_retry_wait_elapses_after_restart(tmp_path: Path) -> None:
    """After restart, a retryable unit whose persisted retry-wait has elapsed is claimed.

    Confirms the gate is the durable retry-wait, not a blanket exclusion: a
    retryable-failed unit persisted with an *elapsed* ``earliest_next_attempt_at``
    is eligible to the reconstructed runner and runs to success, while a
    permanent failure persisted alongside it is still never re-claimed.
    """
    db_path = str(tmp_path / "stub-restart-elapsed.db")
    now = dt.datetime.now(dt.timezone.utc)

    engine_a = create_stub_file_engine(db_path, create_schema=True)
    handler_a = StubStageHandler(engine_a, concurrency_limit=2)
    # Seed durable terminal state directly: a permanent failure and a
    # retryable-failed unit whose retry-wait is already in the past.
    perm_id = handler_a.enqueue("perm", status=WorkUnitStatus.FAILED.value)
    ready_id = handler_a.enqueue(
        "ready",
        behavior=succeed,
        status=WorkUnitStatus.FAILED.value,
        earliest_next_attempt_at=now - dt.timedelta(minutes=10),
    )
    # Mark the permanent unit's retry class durably so claim_next excludes it.
    handler_a.mark_retry_class(perm_id, "permanent")
    handler_a.mark_retry_class(ready_id, "retryable")
    engine_a.dispose()

    engine_b = create_stub_file_engine(db_path, create_schema=False)
    handler_b = StubStageHandler(engine_b, concurrency_limit=2)
    # A restarted process re-supplies execution behavior (production ``execute``
    # is always available); the per-unit closures from the first instance are gone.
    handler_b.register_behavior(ready_id, succeed)
    runner_b = StageRunner(handler_b, engine_b, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner_b.run_until_idle()

    assert handler_b.recorded.execute_started == [str(ready_id)], "only the elapsed-retry-wait unit ran"
    assert handler_b.get_status(ready_id) == WorkUnitStatus.SUCCEEDED.value, (
        "elapsed-retry-wait unit succeeded on retry"
    )
    assert handler_b.get_status(perm_id) == WorkUnitStatus.FAILED.value, "permanent failure never re-claimed"
    engine_b.dispose()
