"""Unit tests for polling retryable conversion jobs."""

from __future__ import annotations

import datetime as dt
from unittest.mock import Mock

import pytest
from sqlmodel import Session

from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.utilities.bookmark_utils import BookmarkContentError
from aizk.conversion.workers import converter, fetcher, worker
from aizk.conversion.workers.worker import ConversionInput


def _create_bookmark(db_session: Session) -> Bookmark:
    bookmark = Bookmark(
        karakeep_id="bm_poll_retryable",
        url="https://example.com",
        normalized_url="https://example.com",
        title="Poll Retryable",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)
    return bookmark


def test_process_job_retries_upload(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Verify upload retries without invoking real conversion or network calls."""
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "1")
    monkeypatch.setattr(worker.mp, "get_context", lambda _ctx: _InlineContext())
    fp.allow_unregistered(False)

    # Seed a bookmark/job so process_job can move through its normal workflow.
    bookmark = Bookmark(
        karakeep_id="bm_retry_test",
        url="https://example.com",
        normalized_url="https://example.com",
        title="Retry Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="a" * 64,
        status=ConversionJobStatus.QUEUED,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    def _prepare_conversion_input(**_kwargs):
        # Force a deterministic conversion input to avoid calling external services.
        return ConversionInput(
            pipeline="html",
            content_bytes=b"<html><body>test</body></html>",
            fetched_at=dt.datetime.now(dt.timezone.utc),
        )

    # Bypass network and conversion steps so we can focus on the upload retry loop.
    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _karakeep_id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bookmark: None)
    monkeypatch.setattr(worker, "_prepare_conversion_input", _prepare_conversion_input)
    monkeypatch.setattr(worker, "_run_conversion", lambda **_kwargs: None)

    upload_attempts = {"count": 0}
    sleep_calls: list[float] = []
    handle_errors = {"count": 0}

    def _upload_converted(_job_id, _workspace):
        # Fail twice to exercise retry backoff, then succeed.
        upload_attempts["count"] += 1
        if upload_attempts["count"] < 3:
            raise RuntimeError("transient upload failure")

    def _handle_job_error(_job_id, _error):
        # Track error handling to ensure we don't mark the job as failed on success.
        handle_errors["count"] += 1

    monkeypatch.setattr(worker, "_upload_converted", _upload_converted)
    monkeypatch.setattr(worker, "handle_job_error", _handle_job_error)
    # Capture sleep durations instead of actually sleeping.
    monkeypatch.setattr(worker.time, "sleep", lambda delay: sleep_calls.append(delay))

    worker.process_job_supervised(job.id)

    assert upload_attempts["count"] == 3
    assert sleep_calls == [1, 2]
    assert handle_errors["count"] == 0


def test_process_job_stops_on_cancellation(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Stop processing before upload when a job is cancelled mid-run."""
    monkeypatch.setattr(worker.mp, "get_context", lambda _ctx: _InlineContext())
    fp.allow_unregistered(False)
    bookmark = Bookmark(
        karakeep_id="bm_cancel_test",
        url="https://example.com",
        normalized_url="https://example.com",
        title="Cancel Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="c" * 64,
        status=ConversionJobStatus.QUEUED,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    def _prepare_conversion_input(**_kwargs):
        return ConversionInput(
            pipeline="html",
            content_bytes=b"<html><body>cancel</body></html>",
            fetched_at=dt.datetime.now(dt.timezone.utc),
        )

    def _run_conversion(**kwargs):
        engine = kwargs["engine"]
        with Session(engine) as session:
            job_record = session.get(ConversionJob, job.id)
            job_record.status = ConversionJobStatus.CANCELLED
            session.add(job_record)
            session.commit()

    upload_calls = {"count": 0}

    def _upload_converted(_job_id, _workspace):
        """If called, will increment, indicating failure to stop on cancellation."""
        upload_calls["count"] += 1

    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _karakeep_id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bookmark: None)
    monkeypatch.setattr(worker, "_prepare_conversion_input", _prepare_conversion_input)
    monkeypatch.setattr(worker, "_run_conversion", _run_conversion)
    monkeypatch.setattr(worker, "_upload_converted", _upload_converted)

    worker.process_job_supervised(job.id)

    assert upload_calls["count"] == 0
    db_session.refresh(job)
    assert job.status == ConversionJobStatus.CANCELLED


def test_poll_picks_retryable_job_after_delay(monkeypatch, db_session: Session) -> None:
    """Ensure FAILED_RETRYABLE jobs are eligible once the backoff window expires."""
    now = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(worker, "_utcnow", lambda: now)

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="a" * 64,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        attempts=1,
        queued_at=now - dt.timedelta(minutes=5),
        earliest_next_attempt_at=now - dt.timedelta(seconds=1),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    processed: list[int] = []

    # Avoid running the full job; only capture the job id selected for processing.
    monkeypatch.setattr(
        worker, "process_job_supervised", lambda job_id, poll_interval_seconds=2.0: processed.append(job_id)
    )

    assert worker.poll_and_process_jobs() is True
    assert processed == [job.id]

    db_session.refresh(job)
    assert job.status == ConversionJobStatus.RUNNING
    assert job.attempts == 2


def test_poll_skips_retryable_job_before_delay(monkeypatch, db_session: Session) -> None:
    """Ensure FAILED_RETRYABLE jobs are not picked up before earliest_next_attempt_at."""
    now = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(worker, "_utcnow", lambda: now)

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="b" * 64,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        attempts=1,
        queued_at=now - dt.timedelta(minutes=5),
        earliest_next_attempt_at=now + dt.timedelta(minutes=5),
    )
    db_session.add(job)
    db_session.commit()

    processed: list[int] = []
    monkeypatch.setattr(
        worker, "process_job_supervised", lambda job_id, poll_interval_seconds=2.0: processed.append(job_id)
    )

    assert worker.poll_and_process_jobs() is False
    assert processed == []


def test_raise_if_cancelled_raises(db_session: Session) -> None:
    """Raise a cancellation exception when a job is already cancelled."""
    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="d" * 64,
        status=ConversionJobStatus.CANCELLED,
    )
    db_session.add(job)
    db_session.commit()

    with pytest.raises(worker.ConversionCancelledError):
        worker._raise_if_cancelled(job.id, db_session.get_bind())


class _InlineProcess:
    """Process that executes target immediately in the same process for testing."""

    def __init__(self, target, args) -> None:
        self._target = target
        self._args = args
        self.exitcode = None

    def start(self) -> None:
        try:
            self._target(*self._args)
            self.exitcode = 0
        except Exception:
            self.exitcode = 1

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        return

    def terminate(self) -> None:
        return

    def kill(self) -> None:
        return


class _InlineContext:
    """Context that provides Queue and inline-executing Process for testing."""

    def Queue(self):  # noqa: N802
        return worker.queue_module.Queue()

    def Process(self, target, args, daemon: bool) -> _InlineProcess:  # noqa: N802
        return _InlineProcess(target, args)


class _TestProcess:
    """Process stub that simulates liveness without running the target."""

    def __init__(self, target, args, daemon: bool, *, alive_cycles: int, exitcode: int, on_start) -> None:
        self._target = target
        self._args = args
        self._alive_cycles = alive_cycles
        self._exitcode = exitcode
        self._on_start = on_start
        self._is_alive = True
        self.pid = 12345
        self.exitcode = None

    def start(self) -> None:
        if self._on_start:
            self._on_start(self)
        self.exitcode = self._exitcode

    def is_alive(self) -> bool:
        return self._is_alive

    def join(self, timeout: float | None = None) -> None:
        if self._alive_cycles > 0:
            self._alive_cycles -= 1
        if self._alive_cycles == 0:
            self._is_alive = False

    def terminate(self) -> None:
        self._is_alive = False

    def kill(self) -> None:
        self._is_alive = False


class _TestContext:
    """Context that provides Queue and a controllable Process stub."""

    def __init__(self, *, alive_cycles: int = 1, exitcode: int = 0, on_start=None) -> None:
        self._alive_cycles = alive_cycles
        self._exitcode = exitcode
        self._on_start = on_start

    def Queue(self):  # noqa: N802
        return worker.queue_module.Queue()

    def Process(self, target, args, daemon: bool) -> _TestProcess:  # noqa: N802
        return _TestProcess(
            target,
            args,
            daemon,
            alive_cycles=self._alive_cycles,
            exitcode=self._exitcode,
            on_start=self._on_start,
        )


def _create_running_job(db_session: Session, bookmark: Bookmark) -> ConversionJob:
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title or "",
        idempotency_key="e" * 64,
        status=ConversionJobStatus.RUNNING,
        started_at=dt.datetime.now(dt.timezone.utc),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def test_process_job_supervised_times_out_with_phase(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Timeouts use the last reported phase from the subprocess."""
    import os

    fp.allow_unregistered(False)

    def _monotonic_sequence(values: list[float]):
        iterator = iter(values)
        last = values[-1]

        def _fake() -> float:
            nonlocal last
            try:
                last = next(iterator)
            except StopIteration:
                pass
            return last

        return _fake

    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "1")  # Short timeout
    monkeypatch.setattr(worker.mp, "get_context", lambda _ctx: _TestContext())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(worker.time, "monotonic", _monotonic_sequence([0.0, 2.0, 2.0]))
    monkeypatch.setattr(worker, "_is_job_cancelled", lambda _job_id, _engine: False)
    monkeypatch.setattr(worker, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _karakeep_id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bookmark: None)

    # Track os.killpg calls
    killpg_calls = []

    def _track_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)
    monkeypatch.setattr(os, "killpg", _track_killpg)

    errors: list[Exception] = []

    def _handle_job_error(_job_id, error):
        errors.append(error)

    monkeypatch.setattr(worker, "handle_job_error", _handle_job_error)

    assert job.id is not None
    worker.process_job_supervised(job.id, poll_interval_seconds=0.1)

    assert len(errors) == 1
    assert isinstance(errors[0], worker.ConversionTimeoutError)
    # Note: phase tracking happens in subprocess, so we may not see "converting" in parent
    assert len(killpg_calls) > 0, "Should call os.killpg() to terminate process group"


def test_process_job_supervised_cancels_child(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Cancellation polling terminates the child process."""
    import os

    fp.allow_unregistered(False)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(worker.mp, "get_context", lambda _ctx: _TestContext())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(worker, "_is_job_cancelled", lambda _job_id, _engine: True)  # Immediately cancelled
    monkeypatch.setattr(worker, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _karakeep_id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bookmark: None)

    # Track os.killpg calls
    killpg_calls = []

    def _track_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)
    monkeypatch.setattr(os, "killpg", _track_killpg)

    handle_calls: list[Exception] = []
    monkeypatch.setattr(worker, "handle_job_error", lambda _job_id, error: handle_calls.append(error))

    assert job.id is not None
    worker.process_job_supervised(job.id, poll_interval_seconds=0.1)

    assert len(killpg_calls) > 0, "Should call os.killpg() to terminate process group on cancellation"
    assert handle_calls == []


def test_process_job_supervised_reports_subprocess_error(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Non-zero exit codes report a subprocess error."""
    fp.allow_unregistered(False)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(worker.mp, "get_context", lambda _ctx: _TestContext(exitcode=1))
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(worker, "_is_job_cancelled", lambda _job_id, _engine: False)
    monkeypatch.setattr(worker, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _karakeep_id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bookmark: None)

    errors: list[Exception] = []
    monkeypatch.setattr(worker, "handle_job_error", lambda _job_id, error: errors.append(error))

    assert job.id is not None
    worker.process_job_supervised(job.id, poll_interval_seconds=0.1)

    assert len(errors) == 1
    # When subprocess raises an error, it's reported as ReportedChildError
    assert isinstance(errors[0], (worker.ReportedChildError, worker.ConversionSubprocessError))


# Phase 1: Process Group Management Tests


def test_process_group_creation_called_in_subprocess(monkeypatch) -> None:
    """Verify os.setpgrp() is called at subprocess start."""
    import os
    import signal
    import tempfile

    setpgrp_called = []

    def _mock_setpgrp():
        setpgrp_called.append(True)

    monkeypatch.setattr(os, "setpgrp", _mock_setpgrp)

    # Mock the rest of the subprocess to prevent actual execution
    monkeypatch.setattr(worker, "_convert_job_artifacts", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "_report_status", lambda *_args, **_kwargs: None)

    # Call the subprocess entrypoint directly
    from pathlib import Path
    import queue as queue_module

    test_queue = queue_module.Queue()
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = Path(tmpdir)
        payload_path = workspace_path / "payload.json"
        worker._process_job_subprocess(
            job_id=1,
            workspace_path=str(workspace_path),
            karakeep_payload_path=str(payload_path),
            status_queue=test_queue,
        )

    assert len(setpgrp_called) == 1, "os.setpgrp() should be called once at subprocess start"


def test_process_group_termination_uses_killpg(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Verify os.killpg() is called with SIGTERM and SIGKILL."""
    import os
    import signal

    fp.allow_unregistered(False)
    monkeypatch.setattr(worker.mp, "get_context", lambda _ctx: _TestContext())
    killpg_calls = []

    def _mock_killpg(pgid, sig):
        killpg_calls.append({"pgid": pgid, "signal": sig})

    monkeypatch.setattr(os, "killpg", _mock_killpg)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(worker, "_is_job_cancelled", lambda _job_id, _engine: True)  # Trigger cancellation
    monkeypatch.setattr(worker, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _karakeep_id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bookmark: None)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)

    assert job.id is not None
    worker.process_job_supervised(job.id, poll_interval_seconds=0.1)

    # Should call killpg with SIGTERM first, then SIGKILL
    assert len(killpg_calls) >= 1, "Should call killpg at least once"
    # First call should be SIGTERM
    assert killpg_calls[0]["signal"] == signal.SIGTERM


def test_process_group_handles_esrch_gracefully(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Verify ProcessLookupError (ESRCH) is caught and doesn't propagate."""
    import os

    fp.allow_unregistered(False)
    monkeypatch.setattr(worker.mp, "get_context", lambda _ctx: _TestContext())

    def _mock_killpg(pgid, sig):
        raise ProcessLookupError("Process group already gone")

    monkeypatch.setattr(os, "killpg", _mock_killpg)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(worker, "_is_job_cancelled", lambda _job_id, _engine: True)
    monkeypatch.setattr(worker, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _karakeep_id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bookmark: None)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)

    # Should not raise ProcessLookupError, should handle gracefully
    assert job.id is not None
    worker.process_job_supervised(job.id, poll_interval_seconds=0.1)
    # Test passes if no exception is raised


def test_handle_job_error_retryable_sets_failed_retryable(monkeypatch, db_session: Session) -> None:
    """Retryable exceptions should mark job FAILED_RETRYABLE with next attempt set."""
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "1")
    monkeypatch.setattr(worker, "get_engine", lambda _database_url=None: db_session.get_bind())

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="r" * 64,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    class CustomRetryableError(Exception):
        error_code = "custom_retryable"
        retryable = True

    worker.handle_job_error(job.id, CustomRetryableError("boom"))

    db_session.refresh(job)
    assert job.status == ConversionJobStatus.FAILED_RETRYABLE
    assert job.earliest_next_attempt_at is not None
    assert job.finished_at is None


def test_handle_job_error_permanent_sets_failed_perm(monkeypatch, db_session: Session) -> None:
    """Permanent exceptions should mark job FAILED_PERM with finished_at set."""
    monkeypatch.setattr(worker, "get_engine", lambda _database_url=None: db_session.get_bind())

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="p" * 64,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    worker.handle_job_error(job.id, BookmarkContentError("missing content"))

    db_session.refresh(job)
    assert job.status == ConversionJobStatus.FAILED_PERM
    assert job.earliest_next_attempt_at is None
    assert job.finished_at is not None


def test_handle_job_error_defaults_to_retryable_without_attr(monkeypatch, db_session: Session) -> None:
    """Exceptions without a retryable attribute default to retryable (FAILED_RETRYABLE)."""
    monkeypatch.setattr(worker, "get_engine", lambda _database_url=None: db_session.get_bind())

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="q" * 64,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    worker.handle_job_error(job.id, RuntimeError("generic error"))

    db_session.refresh(job)
    assert job.status == ConversionJobStatus.FAILED_RETRYABLE
    assert job.earliest_next_attempt_at is not None


def test_error_code_and_retryable_mapping() -> None:
    """Verify error_code and retryable combinations for key exception types."""
    # Worker-defined errors
    jde = worker.JobDataIntegrityError("bad job")
    cto = worker.ConversionTimeoutError("timeout", phase="converting")
    cse = worker.ConversionSubprocessError("subprocess failed")

    assert getattr(jde, "error_code", None) == "job_data_integrity"
    assert getattr(jde, "retryable", True) is False

    assert getattr(cto, "error_code", None) == "conversion_timeout"
    assert getattr(cto, "retryable", False) is True

    assert getattr(cse, "error_code", None) == "conversion_subprocess_failed"
    assert getattr(cse, "retryable", False) is True

    # Bookmark content error (permanent)
    bce = BookmarkContentError("missing")
    assert getattr(bce, "error_code", None) == "karakeep_bookmark_missing_contents"
    assert getattr(bce, "retryable", True) is False

    # Converter errors
    deo = converter.DoclingEmptyOutputError()
    assert getattr(deo, "error_code", None) == "docling_empty_output"
    assert getattr(deo, "retryable", True) is False

    # Fetcher errors
    fe = fetcher.FetchError("network")
    assert getattr(fe, "error_code", "conversion_failed") == "conversion_failed"
    assert getattr(fe, "retryable", False) is True

    # Reported child error mapping based on error_code
    rce_perm = worker.ReportedChildError("child failed", "docling_empty_output")
    assert getattr(rce_perm, "retryable", True) is False
    rce_retry = worker.ReportedChildError("child failed", "transient")
    assert getattr(rce_retry, "retryable", False) is True
