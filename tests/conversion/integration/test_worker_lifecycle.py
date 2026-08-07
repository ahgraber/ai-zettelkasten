"""Integration tests for worker subprocess lifecycle with real process management.

Exercises the REAL subprocess termination path through the runner adapter
(:class:`~aizk.conversion.handler.ConversionStageHandler.execute`), which
spawns the conversion subprocess via ``_spawn_and_supervise`` and lets the
supervision loop — the single owner of the ``mp.Process`` — perform the
graceful-before-forceful (SIGTERM → wait → SIGKILL) process-group termination.

Cancellation here is driven cooperatively through the adapter's
``is_cancelled_fn`` seam (the DB-status poll ``_is_job_cancelled``), the same
seam the runner's per-unit cancel path ultimately observes. The runner-driven
*deadline* termination (and grandchild reaping under a wall-clock timeout) is
covered separately by ``test_handler_runner_lifecycle.py``; the runner
real-subprocess SUCCEEDED + drain proof lives in ``test_worker_e2e.py``.

NOTE: These tests spawn real subprocesses and use real signal handling.
They require pytest-isolate to run safely:
    pip install pytest-isolate
    pytest -m integration_lifecycle
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import time

import psutil
import pytest
from sqlmodel import Session

from aizk.conversion import handler as repository_mod, queries
from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as Bookmark
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.processing import errors as errors_mod, subproc
from aizk.conversion.utilities.config import ConversionConfig
from tests.conversion.integration import _subprocess_helpers
from tests.conversion.integration._zombie_checks import ZombieInspectionError, descendant_zombies

# Mark all tests in this module to run in isolated process.
# Incompatible with xdist — use -m "not integration_lifecycle" when running with -n auto.
pytestmark = [
    pytest.mark.isolate,  # Requires pytest-isolate: pip install pytest-isolate
    pytest.mark.integration_lifecycle,  # Custom marker for selective running
]


# Aliases into the minimal-imports helper module.
# These functions are passed as spawn() targets, so the child process reimports their defining module.
# Keeping them in _subprocess_helpers (stdlib-only) avoids loading the full aizk/CUDA graph on every spawn,
# which would otherwise exceed short test timeouts on GPU machines.
_test_process_subprocess = _subprocess_helpers.test_process_subprocess
_process_job_subprocess_spawn_child = _subprocess_helpers.process_job_subprocess_spawn_child
_process_job_subprocess_graceful_sigterm = _subprocess_helpers.process_job_subprocess_graceful_sigterm
_process_job_subprocess_ignore_sigterm = _subprocess_helpers.process_job_subprocess_ignore_sigterm


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
    """Fail if the job's subprocess was left un-reaped by this process.

    A host that will not let the process table be read cannot answer the
    question, so the test skips rather than passing on an inspection that never
    ran.
    """
    try:
        zombies = descendant_zombies()
    except ZombieInspectionError as exc:
        pytest.skip(f"cannot verify subprocess reaping on this host: {exc}")
    if zombies:
        pytest.fail(f"Job {job_id} left zombie processes: {'; '.join(zombies)}")


def _wait_for_path(path: Path, *, timeout_seconds: float, interval_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(interval_seconds)
    pytest.fail(f"Expected {path} to exist within {timeout_seconds} seconds")


def _make_fake_runtime():
    from unittest.mock import MagicMock

    from aizk.conversion.wiring.worker import WorkerRuntime

    fake_caps = MagicMock()
    fake_caps.converter_requires_gpu.return_value = False
    return WorkerRuntime(
        coordinator=MagicMock(),
        resource_guard=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)),
        capabilities=fake_caps,
    )


def _create_test_bookmark(db_session: Session) -> Bookmark:
    """Helper to create a test bookmark."""
    _ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_lifecycle_test")
    bookmark = Bookmark(
        karakeep_id="bm_lifecycle_test",
        source_ref=_ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref),
        owner_id="self",
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


def _create_running_job(db_session: Session, bookmark: Bookmark) -> ConversionJob:
    """Seed an already-claimed RUNNING job, as ``claim_next`` would leave it.

    ``execute`` is entered after the claim, so the job arrives RUNNING with a
    valid ``source_ref`` and ``attempts`` already incremented.
    """
    source_ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id=bookmark.karakeep_id or "bm_lifecycle_test")
    job = ConversionJob(
        source_id=bookmark.source_id,
        owner_id="self",
        title=bookmark.title or "Test Job",
        idempotency_key="lifecycle" * 8,
        status=ConversionJobStatus.RUNNING,
        attempts=1,
        started_at=dt.datetime.now(dt.timezone.utc),
        source_ref=source_ref.model_dump_json(),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def _track_spawn(monkeypatch, target) -> list:
    """Patch the subprocess target + track the spawned ``mp.Process`` instances.

    ``subproc._spawn_conversion_subprocess`` calls ``ctx.Process`` with the
    ``target``/``args``/``daemon`` keyword arguments, so the tracking wrapper must
    accept them by keyword.
    """
    spawned: list = []
    original_process_class = subproc.mp.get_context("spawn").Process

    def _track_process(*, target, args, daemon):
        proc = original_process_class(target=target, args=args, daemon=daemon)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(subproc, "_process_job_subprocess", target)
    ctx = subproc.mp.get_context("spawn")
    monkeypatch.setattr(ctx, "Process", _track_process)
    return spawned


def _get_spawned_pid(spawned: list) -> int:
    """Assert exactly one subprocess was spawned with a valid PID and return it."""
    assert len(spawned) == 1, "Should have spawned one subprocess"
    pid = spawned[0].pid
    assert pid is not None, "Subprocess should have been spawned"
    return pid


def test_real_subprocess_spawned_and_terminated(monkeypatch, db_session: Session) -> None:
    """``execute`` spawns a real subprocess and the supervision loop terminates it on cancel."""
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "10")

    bookmark = _create_test_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    spawned = _track_spawn(monkeypatch, _test_process_subprocess)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())

    # Cancel job after it starts to trigger cooperative termination.
    cancel_state = {"called": False}

    def _mock_is_cancelled(_job_id, _engine):
        if not cancel_state["called"]:
            cancel_state["called"] = True
            return False
        return True

    monkeypatch.setattr(repository_mod, "_is_job_cancelled", _mock_is_cancelled)

    config = ConversionConfig(_env_file=None)
    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    with pytest.raises(errors_mod.ConversionCancelledError):
        handler.execute(job.id)

    spawned_pid = _get_spawned_pid(spawned)

    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_cancelled_job_terminates_subprocess_with_no_zombies(monkeypatch, db_session: Session) -> None:
    """Cancelling a job terminates the subprocess and leaves no zombie processes."""
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "10")

    bookmark = _create_test_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    spawned = _track_spawn(monkeypatch, _test_process_subprocess)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda _job_id, _engine: True)

    config = ConversionConfig(_env_file=None)
    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    with pytest.raises(errors_mod.ConversionCancelledError):
        handler.execute(job.id)

    spawned_pid = _get_spawned_pid(spawned)
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_subprocess_completes_normally_no_zombies(
    monkeypatch,
    db_session: Session,
) -> None:
    """A subprocess that completes normally leaves no zombie processes."""
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "0.1")

    bookmark = _create_test_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    spawned = _track_spawn(monkeypatch, _test_process_subprocess)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda _job_id, _engine: False)
    # Skip the in-process upload phase; this test only cares about clean reaping.
    monkeypatch.setattr(repository_mod, "_prepare_upload", lambda *_a, **_k: None)

    config = ConversionConfig(_env_file=None)
    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    handler.execute(job.id)

    spawned_pid = _get_spawned_pid(spawned)
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    assert spawned[0].exitcode == 0
    _assert_no_zombie_processes(job.id)


def test_process_group_terminates_grandchild(
    monkeypatch,
    db_session: Session,
    tmp_path: Path,
) -> None:
    """Process-group termination kills child and grandchild processes."""
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")
    pid_file = tmp_path / "worker_child_pids.txt"
    monkeypatch.setenv("WORKER_TEST_PID_FILE", str(pid_file))

    bookmark = _create_test_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    spawned = _track_spawn(monkeypatch, _process_job_subprocess_spawn_child)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())

    def _cancel_when_ready(_job_id, _engine):
        return pid_file.exists()

    monkeypatch.setattr(repository_mod, "_is_job_cancelled", _cancel_when_ready)

    config = ConversionConfig(_env_file=None)
    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    with pytest.raises(errors_mod.ConversionCancelledError):
        handler.execute(job.id)

    spawned_pid = _get_spawned_pid(spawned)

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
    tmp_path: Path,
) -> None:
    """SIGTERM shutdown completes within the grace period."""
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")
    marker_path = tmp_path / "sigterm_marker.txt"
    monkeypatch.setenv("WORKER_TEST_MARKER_PATH", str(marker_path))
    ready_path = tmp_path / "sigterm_ready.txt"
    monkeypatch.setenv("WORKER_TEST_READY_PATH", str(ready_path))

    bookmark = _create_test_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    spawned = _track_spawn(monkeypatch, _process_job_subprocess_graceful_sigterm)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())

    cancel_state = {"cancel_time": None}

    def _mock_is_cancelled(_job_id, _engine):
        if ready_path.exists():
            if cancel_state["cancel_time"] is None:
                cancel_state["cancel_time"] = time.monotonic()
            return True
        return False

    monkeypatch.setattr(repository_mod, "_is_job_cancelled", _mock_is_cancelled)

    config = ConversionConfig(_env_file=None)
    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    with pytest.raises(errors_mod.ConversionCancelledError):
        handler.execute(job.id)

    if cancel_state["cancel_time"] is not None:
        cancel_elapsed = time.monotonic() - cancel_state["cancel_time"]
        assert cancel_elapsed <= 5.0
    _wait_for_path(marker_path, timeout_seconds=2.0, interval_seconds=0.05)

    spawned_pid = _get_spawned_pid(spawned)
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_sigkill_after_sigterm_when_subprocess_ignores_sigterm(monkeypatch, db_session: Session) -> None:
    """SIGKILL is sent after SIGTERM when the subprocess ignores termination.

    Drives a cooperative cancel against a subprocess that ignores SIGTERM; the
    supervision loop's ``_terminate_and_wait`` must escalate to SIGKILL (waiting
    out the 5s SIGTERM grace) so the process is still reaped with no zombie.
    """
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")

    bookmark = _create_test_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    spawned = _track_spawn(monkeypatch, _process_job_subprocess_ignore_sigterm)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())

    # Let the subprocess install its SIG_IGN handler before cancel fires.
    cancel_state = {"called": 0}

    def _mock_is_cancelled(_job_id, _engine):
        cancel_state["called"] += 1
        return cancel_state["called"] >= 2

    monkeypatch.setattr(repository_mod, "_is_job_cancelled", _mock_is_cancelled)

    config = ConversionConfig(_env_file=None)
    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    with pytest.raises(errors_mod.ConversionCancelledError):
        handler.execute(
            job.id,
        )

    spawned_pid = _get_spawned_pid(spawned)
    # SIGTERM is ignored, so termination only completes after the SIGKILL
    # escalation (post-5s grace); the pid must still be gone with no zombie.
    _assert_pid_gone(spawned_pid, timeout_seconds=15.0, interval_seconds=0.1)
    _assert_no_zombie_processes(job.id)


def test_cancel_mid_execution_terminates_within_poll_interval(
    monkeypatch,
    db_session: Session,
) -> None:
    """Cancellation ends the subprocess within roughly the poll interval."""
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "10")

    bookmark = _create_test_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    spawned = _track_spawn(monkeypatch, _test_process_subprocess)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())

    cancel_state = {"called": 0, "cancel_time": None}

    def _mock_is_cancelled(_job_id, _engine):
        cancel_state["called"] += 1
        if cancel_state["called"] >= 2:
            if cancel_state["cancel_time"] is None:
                cancel_state["cancel_time"] = time.monotonic()
            return True
        return False

    monkeypatch.setattr(repository_mod, "_is_job_cancelled", _mock_is_cancelled)

    # The adapter hardcodes the supervision poll interval at 2.0s; bound the
    # elapsed-cancel assertion to that plus margin.
    config = ConversionConfig(_env_file=None)
    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    with pytest.raises(errors_mod.ConversionCancelledError):
        handler.execute(job.id)

    assert cancel_state["cancel_time"] is not None
    cancel_elapsed = time.monotonic() - cancel_state["cancel_time"]
    assert cancel_elapsed <= 2.0 + 0.5

    spawned_pid = _get_spawned_pid(spawned)
    _assert_pid_gone(spawned_pid, timeout_seconds=5.0, interval_seconds=0.05)
    _assert_no_zombie_processes(job.id)


def test_recover_stale_running_job_marks_retryable(monkeypatch, db_session: Session) -> None:
    """Stale running jobs are reclaimed to FAILED_RETRYABLE via the shared recovery query."""
    monkeypatch.setenv("AIZK_WORKER_STALE_JOB_MINUTES", "0")

    bookmark = _create_test_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)
    job.started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    config = ConversionConfig(_env_file=None)
    # ``recover_stale_in_session`` runs inside a caller-owned transaction; the
    # runner owns the commit. Drive it directly here and commit.
    recovered = queries.recover_stale_in_session(db_session, config)
    db_session.commit()

    assert recovered == [job.id]
    db_session.refresh(job)
    assert job.status == ConversionJobStatus.FAILED_RETRYABLE
    assert job.error_code == "worker_stale_running"
    assert job.earliest_next_attempt_at is not None
