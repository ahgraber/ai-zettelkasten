"""Integration tests for worker subprocess lifecycle with real process management.

Tests the actual AIZK worker code (process_job_supervised) with real subprocesses
to verify cancellation, timeout, and cleanup behavior.

NOTE: These tests spawn real subprocesses and use real signal handling.
They require pytest-isolate to run safely:
    pip install pytest-isolate
    pytest tests/conversion/integration/test_worker_lifecycle.py

Or run with custom marker:
    pytest -m integration_lifecycle
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import psutil
import pytest
from sqlmodel import Session

from aizk.conversion.datamodel.source import Source
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import errors as errors_mod, loop, orchestrator

# Mark all tests in this module to run in isolated process.
# Incompatible with xdist — use -m "not integration_lifecycle" when running with -n auto.
pytestmark = [
    pytest.mark.isolate,  # Requires pytest-isolate: pip install pytest-isolate
    pytest.mark.integration_lifecycle,  # Custom marker for selective running
]


def _test_process_subprocess(
    job_id: int,
    workspace_path: str,
    karakeep_payload_path: str,
    status_queue,
) -> None:
    import time

    try:
        os.setpgrp()
    except OSError:
        pass
    if status_queue:
        status_queue.put_nowait({"event": "phase", "message": "converting"})
    sleep_seconds = float(os.getenv("WORKER_TEST_SLEEP_SECONDS", "0"))
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    if status_queue:
        status_queue.put_nowait({"event": "completed", "message": "conversion completed"})


def _process_job_subprocess_spawn_child(
    job_id: int,
    workspace_path: str,
    karakeep_payload_path: str,
    status_queue,
) -> None:
    from pathlib import Path
    import time

    try:
        os.setpgrp()
    except OSError:
        pass
    if status_queue:
        status_queue.put_nowait({"event": "phase", "message": "converting"})
    pid_file = os.environ.get("WORKER_TEST_PID_FILE")
    if not pid_file:
        raise RuntimeError("Missing WORKER_TEST_PID_FILE")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603
    Path(pid_file).write_text(f"{os.getpid()},{child.pid}")
    time.sleep(60)


def _process_job_subprocess_graceful_sigterm(
    job_id: int,
    workspace_path: str,
    karakeep_payload_path: str,
    status_queue,
) -> None:
    from pathlib import Path
    import signal
    import time

    try:
        os.setpgrp()
    except OSError:
        pass
    marker = os.environ.get("WORKER_TEST_MARKER_PATH")
    if not marker:
        raise RuntimeError("Missing WORKER_TEST_MARKER_PATH")
    ready_marker = os.environ.get("WORKER_TEST_READY_PATH")
    if ready_marker:
        Path(ready_marker).write_text("ready")

    def _handle(_signum, _frame):
        Path(marker).write_text("terminated")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle)
    if status_queue:
        status_queue.put_nowait({"event": "phase", "message": "converting"})
    while True:
        time.sleep(1)


def _process_job_subprocess_ignore_sigterm(
    job_id: int,
    workspace_path: str,
    karakeep_payload_path: str,
    status_queue,
) -> None:
    import signal
    import time

    try:
        os.setpgrp()
    except OSError:
        pass
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if status_queue:
        status_queue.put_nowait({"event": "phase", "message": "converting"})
    while True:
        time.sleep(1)


def _assert_pid_gone(pid: int, *, timeout_seconds: float, interval_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        try:
            proc = psutil.Process(pid)
            last_status = proc.status()
        except psutil.NoSuchProcess:
            return
        time.sleep(interval_seconds)
    if last_status == psutil.STATUS_ZOMBIE:
        pytest.fail(f"Process {pid} should not be zombie")
    pytest.fail(f"Process {pid} still exists with status {last_status}")


def _assert_no_zombie_processes(job_id: int) -> None:
    zombies: list[str] = []
    try:
        for proc in psutil.process_iter(["pid", "status", "cmdline"], ad_value=None):
            if proc.info.get("status") == psutil.STATUS_ZOMBIE:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                zombies.append(f"pid={proc.info['pid']} cmdline={cmdline}")
    except PermissionError:
        return
    if zombies:
        formatted = "; ".join(zombies)
        pytest.fail(f"Job {job_id} left zombie processes: {formatted}")


def _assert_no_temp_directories(prefix: str) -> None:
    temp_root = Path(tempfile.gettempdir())
    matches = [path for path in temp_root.iterdir() if path.is_dir() and path.name.startswith(prefix)]
    if matches:
        match_list = ", ".join(path.name for path in matches)
        pytest.fail(f"Temporary directories still exist: {match_list}")


def _wait_for_path(path: Path, *, timeout_seconds: float, interval_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(interval_seconds)
    pytest.fail(f"Expected {path} to exist within {timeout_seconds} seconds")


def _create_test_bookmark(db_session: Session) -> Source:
    """Helper to create a test bookmark."""
    bookmark = Source.from_karakeep_id(
        karakeep_id="bm_lifecycle_test",
        url="https://example.com/test",
        normalized_url="https://example.com/test",
        title="Lifecycle Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)
    return bookmark


def _create_test_job(db_session: Session, bookmark: Source, status: ConversionJobStatus) -> ConversionJob:
    """Helper to create a test job."""
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title=bookmark.title or "Test Job",
        idempotency_key="lifecycle" * 8,
        status=status,
        payload_version=1,
    )
    if status == ConversionJobStatus.RUNNING:
        job.started_at = dt.datetime.now(dt.timezone.utc)
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def test_real_subprocess_spawned_and_terminated(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Verify process_job_supervised spawns real subprocess and can terminate it."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "10")

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    # Track subprocess by storing reference
    spawned_process = []

    original_process_class = orchestrator.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    # Mock helper functions but let subprocess actually spawn
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _test_process_subprocess)

    # Mock mp.Process to track PID
    ctx = orchestrator.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    # Cancel job after it starts to trigger termination
    cancel_state = {"called": False}

    def _mock_is_cancelled(job_id, engine):
        # Return True after first check to trigger cancellation
        if not cancel_state["called"]:
            cancel_state["called"] = True
            return False
        return True

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", _mock_is_cancelled)

    # Run the job
    poll_interval_seconds = 0.1
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=poll_interval_seconds)

    # Verify subprocess was spawned
    assert len(spawned_process) == 1, "Should have spawned one subprocess"
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None, "Subprocess should have been spawned"

    # Verify subprocess is terminated (give it a moment)
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_cancelled_job_terminates_subprocess_with_no_zombies(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Verify cancelling a job terminates subprocess and leaves no zombie processes."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "10")

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = orchestrator.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _test_process_subprocess)

    ctx = orchestrator.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    # Trigger immediate cancellation
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)

    poll_interval_seconds = 1
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=poll_interval_seconds)

    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_timeout_terminates_subprocess(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Verify timeout terminates subprocess near the configured deadline."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "5")  # Short timeout for test
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "10")

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = orchestrator.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _test_process_subprocess)

    ctx = orchestrator.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)

    # Mock handle_job_error to capture timeout error
    errors = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    poll_interval_seconds = 0.1
    config = ConversionConfig(_env_file=None)
    start = time.monotonic()
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=poll_interval_seconds)
    duration = time.monotonic() - start

    # Verify timeout error was raised
    assert len(errors) == 1
    assert isinstance(errors[0], errors_mod.ConversionTimeoutError)

    # Verify termination happened close to deadline (5s ± 2s tolerance)
    assert 3.0 <= duration <= 7.0

    # Verify subprocess was terminated
    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_subprocess_completes_normally_no_zombies(
    monkeypatch,
    db_session: Session,
    html_bookmark,
) -> None:
    """Verify subprocess that completes normally leaves no zombie processes."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "0.1")

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = orchestrator.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _test_process_subprocess)
    monkeypatch.setattr(orchestrator, "_upload_converted", lambda _job_id, _workspace, _config: None)  # Skip upload

    ctx = orchestrator.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)

    created_paths: list[Path] = []
    prefix = "aizk-worker-test-"

    class _TrackedTemporaryDirectory:
        def __init__(self):
            self.path = Path(tempfile.mkdtemp(prefix=prefix))
            created_paths.append(self.path)

        def __enter__(self) -> str:
            return str(self.path)

        def __exit__(self, exc_type, exc, tb) -> bool:
            shutil.rmtree(self.path, ignore_errors=True)
            return False

    monkeypatch.setattr(orchestrator.tempfile, "TemporaryDirectory", _TrackedTemporaryDirectory)

    poll_interval_seconds = 0.1
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=poll_interval_seconds)

    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    assert spawned_process[0].exitcode == 0
    _assert_no_zombie_processes(job.id)
    assert created_paths
    assert all(not path.exists() for path in created_paths)
    _assert_no_temp_directories(prefix)


def test_process_group_terminates_grandchild(
    monkeypatch,
    db_session: Session,
    html_bookmark,
    tmp_path: Path,
) -> None:
    """Verify process group termination kills child and grandchild processes."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    pid_file = tmp_path / "worker_child_pids.txt"
    monkeypatch.setenv("WORKER_TEST_PID_FILE", str(pid_file))

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = orchestrator.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _process_job_subprocess_spawn_child)

    ctx = orchestrator.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    def _cancel_when_ready(_job_id, _engine):
        return pid_file.exists()

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", _cancel_when_ready)

    poll_interval_seconds = 0.1
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=poll_interval_seconds)

    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None

    assert pid_file.exists(), "Expected PID file for child process"
    parent_pid_str, child_pid_str = pid_file.read_text().strip().split(",", maxsplit=1)
    parent_pid = int(parent_pid_str)
    child_pid = int(child_pid_str)

    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_pid_gone(parent_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_pid_gone(child_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_sigterm_graceful_shutdown_within_grace_period(
    monkeypatch,
    db_session: Session,
    html_bookmark,
    tmp_path: Path,
) -> None:
    """Verify SIGTERM shutdown completes within the grace period."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    marker_path = tmp_path / "sigterm_marker.txt"
    monkeypatch.setenv("WORKER_TEST_MARKER_PATH", str(marker_path))
    ready_path = tmp_path / "sigterm_ready.txt"
    monkeypatch.setenv("WORKER_TEST_READY_PATH", str(ready_path))

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = orchestrator.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _process_job_subprocess_graceful_sigterm)

    ctx = orchestrator.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    cancel_state = {"cancel_time": None}

    def _mock_is_cancelled(_job_id, _engine):
        if ready_path.exists():
            if cancel_state["cancel_time"] is None:
                cancel_state["cancel_time"] = time.monotonic()
            return True
        return False

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", _mock_is_cancelled)

    poll_interval_seconds = 0.1
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=poll_interval_seconds)

    if cancel_state["cancel_time"] is not None:
        cancel_elapsed = time.monotonic() - cancel_state["cancel_time"]
        assert cancel_elapsed <= 5.0
    _wait_for_path(marker_path, timeout_seconds=2.0, interval_seconds=0.05)

    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_sigkill_after_sigterm_on_timeout(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Verify SIGKILL is sent after SIGTERM when subprocess ignores termination."""
    timeout_seconds = 1.0
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", str(timeout_seconds))

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = orchestrator.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _process_job_subprocess_ignore_sigterm)

    ctx = orchestrator.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)

    errors = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    start = time.monotonic()
    poll_interval_seconds = 0.1
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=poll_interval_seconds)
    duration = time.monotonic() - start

    assert len(errors) == 1
    assert isinstance(errors[0], errors_mod.ConversionTimeoutError)
    assert duration >= timeout_seconds
    assert duration <= 9.0

    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_cancel_mid_execution_terminates_within_poll_interval(
    monkeypatch,
    db_session: Session,
    html_bookmark,
) -> None:
    """Verify cancellation ends the subprocess within the poll interval."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "10")

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = orchestrator.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _test_process_subprocess)

    ctx = orchestrator.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    cancel_state = {"called": 0, "cancel_time": None}

    def _mock_is_cancelled(_job_id, _engine):
        cancel_state["called"] += 1
        if cancel_state["called"] >= 2:
            if cancel_state["cancel_time"] is None:
                cancel_state["cancel_time"] = time.monotonic()
            return True
        return False

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", _mock_is_cancelled)

    poll_interval_seconds = 0.1
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=poll_interval_seconds)

    assert cancel_state["cancel_time"] is not None
    cancel_elapsed = time.monotonic() - cancel_state["cancel_time"]
    assert cancel_elapsed <= poll_interval_seconds + 0.5

    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_recover_stale_running_job_marks_retryable(monkeypatch, db_session: Session) -> None:
    """Verify stale running jobs are marked retryable for recovery."""
    monkeypatch.setenv("WORKER_STALE_JOB_MINUTES", "0")

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.RUNNING)
    job.started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    config = ConversionConfig(_env_file=None)
    recovered = loop.recover_stale_running_jobs(config)

    assert recovered == 1
    db_session.refresh(job)
    assert job.status == ConversionJobStatus.FAILED_RETRYABLE
    assert job.error_code == "worker_stale_running"
    assert job.earliest_next_attempt_at is not None
