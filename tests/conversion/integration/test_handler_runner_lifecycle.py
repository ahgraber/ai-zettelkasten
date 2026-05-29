"""Integration test for the runner-driven, single-owner subprocess termination.

This exercises the REAL two-thread termination path that fix B1 introduced:

* The runner driver thread enforces the wall-clock deadline and calls
  ``ConversionStageHandler.cancel(handle)``, which only **signals** a
  per-handle terminate-event — it does NOT join the ``mp.Process``.
* The worker thread, inside ``_supervise_conversion_process`` (the single owner
  of the ``mp.Process``), observes the event each poll iteration and performs the
  graceful-before-forceful process-group termination + join itself.

Before B1 both threads called ``process.join()`` on the same ``mp.Process``
concurrently (runner ``cancel`` → ``_terminate_and_wait`` → ``join`` while the
supervision loop also joined). This test spawns a real subprocess (and a real
grandchild) so it proves: the subprocess and its whole process group are reaped
(no orphan, no zombie), the run does not crash, and ``cancel`` returned promptly
without joining the Process (the subprocess was still alive at the instant
``cancel`` returned — the worker thread did the reaping afterward).

NOTE: spawns real subprocesses with real signal handling; runs isolated under
``integration_lifecycle`` (incompatible with xdist).
"""

from __future__ import annotations

from pathlib import Path
import threading
import time

import psutil
import pytest
from sqlmodel import Session

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as Bookmark
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import orchestrator
from aizk.pipeline.runner import StageRunner
from tests.conversion.integration import _subprocess_helpers

pytestmark = [
    pytest.mark.isolate,  # Requires pytest-isolate: pip install pytest-isolate
    pytest.mark.integration_lifecycle,  # Custom marker for selective running
]

# Spawned as a multiprocessing target, so the child reimports its defining
# (stdlib-only) module. It writes "<parent_pid>,<grandchild_pid>" to
# WORKER_TEST_PID_FILE, then sleeps — long enough that only termination ends it.
_spawn_child_target = _subprocess_helpers.process_job_subprocess_spawn_child


def _assert_pid_gone(pid: int, *, timeout_seconds: float, interval_seconds: float) -> None:
    """Fail unless ``pid`` disappears (and is not left zombie) within the budget."""
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        try:
            last_status = psutil.Process(pid).status()
        except psutil.NoSuchProcess:
            return
        time.sleep(interval_seconds)
    pytest.fail(f"Process {pid} did not exit within {timeout_seconds}s (last status: {last_status})")


def _assert_no_zombie_processes() -> None:
    """Fail if any zombie process is left behind."""
    zombies: list[str] = []
    try:
        for proc in psutil.process_iter(["pid", "status", "cmdline"], ad_value=None):
            if proc.info.get("status") == psutil.STATUS_ZOMBIE:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                zombies.append(f"pid={proc.info['pid']} cmdline={cmdline}")
    except PermissionError:
        return
    if zombies:
        pytest.fail("Left zombie processes: " + "; ".join(zombies))


def _make_fake_runtime():
    """A runtime whose converter never requires the GPU guard."""
    from unittest.mock import MagicMock

    from aizk.conversion.wiring.worker import WorkerRuntime

    fake_caps = MagicMock()
    fake_caps.converter_requires_gpu.return_value = False
    return WorkerRuntime(
        orchestrator=MagicMock(),
        resource_guard=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)),
        capabilities=fake_caps,
    )


def _seed_queued_job(session: Session) -> int:
    """Seed a Source + a QUEUED job the runner will claim and execute."""
    ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_runner_lifecycle")
    bookmark = Bookmark(
        karakeep_id="bm_runner_lifecycle",
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        owner_id="self",
        url="https://example.com/test",
        normalized_url="https://example.com/test",
        title="Runner Lifecycle",
        content_type="html",
        source_type="web",
    )
    session.add(bookmark)
    session.commit()
    session.refresh(bookmark)

    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        owner_id="self",
        title="Runner Lifecycle Job",
        idempotency_key="harnesslc" * 4,
        status=ConversionJobStatus.QUEUED,
        source_ref=ref.model_dump_json(),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    assert job.id is not None
    return job.id


def test_runner_timeout_terminates_subprocess_via_single_owner(
    monkeypatch,
    db_session: Session,
    tmp_path: Path,
) -> None:
    """A runner-enforced timeout terminates the real subprocess via the single owner.

    The runner driver thread enforces the deadline and calls ``cancel`` (which
    only sets the per-handle terminate-event); the worker thread's supervision
    loop — the single owner of the ``mp.Process`` — terminates the process group
    and joins. Asserts the subprocess and its grandchild are reaped (no orphan,
    no zombie), nothing crashes, and ``cancel`` did not itself join the Process
    (it returned while the subprocess was still alive).
    """
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")  # handler.timeout (subprocess phase is runner-owned)
    pid_file = tmp_path / "runner_child_pids.txt"
    monkeypatch.setenv("WORKER_TEST_PID_FILE", str(pid_file))

    config = ConversionConfig(_env_file=None)
    engine = db_session.get_bind()

    _seed_queued_job(db_session)

    # Use the stdlib-only subprocess target (sleeps + spawns a grandchild) and
    # track the spawned mp.Process so we can probe its PID and liveness.
    spawned: list = []
    original_process_class = orchestrator.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _spawn_child_target)
    ctx = orchestrator.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    # The DB-status cancel poll stays inert; termination is driven solely by the
    # runner deadline → cancel → terminate-event single-owner path.
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)

    # Patch finalize to a no-op so the reap path is inert — the termination path
    # under test runs entirely before finalize.
    monkeypatch.setattr(ConversionStageHandler, "finalize", lambda self, session, handle, outcome: None)

    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())

    # Prove cancel does not join: wrap it to snapshot the tracked process's
    # liveness at the instant cancel returns and to time how long cancel took.
    cancel_probe: dict = {"alive_when_cancel_returned": None, "elapsed": None}
    real_cancel = handler.cancel

    def _instrumented_cancel(handle: int) -> None:
        with handler._processes_lock:
            tracked = handler._processes.get(handle)
        start = time.monotonic()
        real_cancel(handle)
        cancel_probe["elapsed"] = time.monotonic() - start
        if tracked is not None:
            # If cancel had joined, the process would be dead here.
            cancel_probe["alive_when_cancel_returned"] = tracked.is_alive()

    monkeypatch.setattr(handler, "cancel", _instrumented_cancel)

    # A short deadline so the runner fires the timeout quickly against the
    # long-sleeping subprocess. clock seam advances the per-unit deadline.
    base = time.monotonic()
    clock_state = {"offset": 0.0}

    def _clock() -> float:
        return base + clock_state["offset"]

    runner = StageRunner(
        handler,
        engine,
        poll_interval=0.05,
        clock=_clock,
        drain_timeout=10.0,
    )

    drain_done = threading.Event()

    def _drive() -> None:
        try:
            runner.run_until_idle(max_iterations=2000)
        finally:
            drain_done.set()

    driver = threading.Thread(target=_drive, name="runner-driver")
    driver.start()

    # Wait for the subprocess to spawn and write its pid file (it is now running).
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.05)
    assert pid_file.exists(), "subprocess spawned and reported its pids"

    parent_pid_str, child_pid_str = pid_file.read_text().strip().split(",", maxsplit=1)
    parent_pid = int(parent_pid_str)
    grandchild_pid = int(child_pid_str)

    # Advance the clock past the per-unit deadline so the runner enforces the
    # timeout and signals cancellation (single-owner terminate-event).
    clock_state["offset"] = 1000.0

    # The runner should drain (worker thread terminates + joins the subprocess).
    assert drain_done.wait(timeout=20.0), "runner drained after the enforced timeout"
    driver.join(timeout=5.0)
    assert not driver.is_alive(), "driver thread exited"

    # The mp.Process, its parent pid, and the grandchild are all reaped — the
    # single-owner termination killed the whole process group, no orphan/zombie.
    assert len(spawned) == 1, "exactly one subprocess was spawned"
    spawned_pid = spawned[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=10.0, interval_seconds=0.05)
    _assert_pid_gone(parent_pid, timeout_seconds=10.0, interval_seconds=0.05)
    _assert_pid_gone(grandchild_pid, timeout_seconds=10.0, interval_seconds=0.05)
    _assert_no_zombie_processes()

    # cancel only signalled: it returned promptly while the subprocess was still
    # alive (the worker thread, not cancel, did the join afterward).
    assert cancel_probe["elapsed"] is not None, "runner invoked the adapter's cancel"
    assert cancel_probe["elapsed"] < 1.0, "cancel returned promptly (it did not block on join)"
    assert cancel_probe["alive_when_cancel_returned"] is True, (
        "cancel did NOT join the Process — it was still alive when cancel returned (single-owner B1)"
    )
