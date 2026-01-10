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
import time

import psutil
import pytest
from sqlmodel import Session

from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.workers import worker

# Mark all tests in this module to run in isolated process
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
    import os
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


def _create_test_bookmark(db_session: Session) -> Bookmark:
    """Helper to create a test bookmark."""
    bookmark = Bookmark(
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


def _create_test_job(db_session: Session, bookmark: Bookmark, status: ConversionJobStatus) -> ConversionJob:
    """Helper to create a test job."""
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
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

    original_process_class = worker.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    # Mock helper functions but let subprocess actually spawn
    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(worker, "_process_job_subprocess", _test_process_subprocess)

    # Mock mp.Process to track PID
    ctx = worker.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    # Cancel job after it starts to trigger termination
    cancel_state = {"called": False}

    def _mock_is_cancelled(job_id, engine):
        # Return True after first check to trigger cancellation
        if not cancel_state["called"]:
            cancel_state["called"] = True
            return False
        return True

    monkeypatch.setattr(worker, "_is_job_cancelled", _mock_is_cancelled)

    # Run the job
    assert job.id is not None
    worker.process_job_supervised(job.id, poll_interval_seconds=0.1)

    # Verify subprocess was spawned
    assert len(spawned_process) == 1, "Should have spawned one subprocess"
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None, "Subprocess should have been spawned"

    # Verify subprocess is terminated (give it a moment)
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)


def test_cancelled_job_terminates_subprocess_with_no_zombies(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Verify cancelling a job terminates subprocess and leaves no zombie processes."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "10")

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = worker.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(worker, "_process_job_subprocess", _test_process_subprocess)

    ctx = worker.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    # Trigger immediate cancellation
    monkeypatch.setattr(worker, "_is_job_cancelled", lambda _job_id, _engine: True)

    assert job.id is not None
    worker.process_job_supervised(job.id, poll_interval_seconds=0.1)

    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)


def test_timeout_terminates_subprocess(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Verify timeout terminates subprocess when deadline exceeded."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "2")  # Short timeout for test
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "10")

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = worker.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(worker, "_process_job_subprocess", _test_process_subprocess)

    ctx = worker.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    monkeypatch.setattr(worker, "_is_job_cancelled", lambda _job_id, _engine: False)

    # Mock handle_job_error to capture timeout error
    errors = []
    monkeypatch.setattr(worker, "handle_job_error", lambda _job_id, error: errors.append(error))

    assert job.id is not None
    worker.process_job_supervised(job.id, poll_interval_seconds=0.1)

    # Verify timeout error was raised
    assert len(errors) == 1
    assert isinstance(errors[0], worker.ConversionTimeoutError)

    # Verify subprocess was terminated
    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)


def test_subprocess_completes_normally_no_zombies(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Verify subprocess that completes normally leaves no zombie processes."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "0.1")

    bookmark = _create_test_bookmark(db_session)
    job = _create_test_job(db_session, bookmark, ConversionJobStatus.QUEUED)

    spawned_process = []
    original_process_class = worker.mp.get_context("spawn").Process

    def _track_process(target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned_process.append(proc)
        return proc

    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bm: None)

    monkeypatch.setattr(worker, "_process_job_subprocess", _test_process_subprocess)
    monkeypatch.setattr(worker, "_upload_converted", lambda _job_id, _workspace: None)  # Skip upload

    ctx = worker.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)

    monkeypatch.setattr(worker, "_is_job_cancelled", lambda _job_id, _engine: False)

    assert job.id is not None
    worker.process_job_supervised(job.id, poll_interval_seconds=0.1)

    assert len(spawned_process) == 1
    spawned_pid = spawned_process[0].pid
    assert spawned_pid is not None
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
