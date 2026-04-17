"""Unit tests for polling retryable conversion jobs."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import queue as queue_module
import time
from unittest.mock import Mock

import pytest
from sqlmodel import Session

from aizk.conversion.datamodel.source import Source
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.storage.s3_client import S3Error, S3UploadError
from aizk.conversion.utilities.bookmark_utils import BookmarkContentError
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import converter, errors as errors_mod, fetcher, loop, orchestrator, uploader
from aizk.conversion.workers.types import ConversionInput, SupervisionResult


def _create_bookmark(db_session: Session) -> Source:
    bookmark = Source.from_karakeep_id(
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
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())
    fp.allow_unregistered(False)

    # Seed a bookmark/job so process_job can move through its normal workflow.
    bookmark = Source.from_karakeep_id(
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
        source_ref=bookmark.source_ref,
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
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _karakeep_id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bookmark: None)
    monkeypatch.setattr(orchestrator, "_prepare_conversion_input", _prepare_conversion_input)
    monkeypatch.setattr(orchestrator, "_run_conversion", lambda **_kwargs: None)

    upload_attempts = {"count": 0}
    sleep_calls: list[float] = []
    handle_errors = {"count": 0}

    def _upload_converted(_job_id, _workspace, _config):
        # Fail twice to exercise retry backoff, then succeed.
        upload_attempts["count"] += 1
        if upload_attempts["count"] < 3:
            raise RuntimeError("transient upload failure")

    def _handle_job_error(_job_id, _error, _config):
        # Track error handling to ensure we don't mark the job as failed on success.
        handle_errors["count"] += 1

    monkeypatch.setattr(orchestrator, "_upload_converted", _upload_converted)
    monkeypatch.setattr(orchestrator, "handle_job_error", _handle_job_error)
    # Capture sleep durations instead of actually sleeping.
    monkeypatch.setattr(orchestrator.time, "sleep", lambda delay: sleep_calls.append(delay))

    config = ConversionConfig(_env_file=None)
    orchestrator.process_job_supervised(job.id, config)

    assert upload_attempts["count"] == 3
    assert sleep_calls == [1, 2]
    assert handle_errors["count"] == 0


def test_process_job_stops_on_cancellation(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Stop processing before upload when a job is cancelled mid-run."""
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())
    fp.allow_unregistered(False)
    bookmark = Source.from_karakeep_id(
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
        source_ref=bookmark.source_ref,
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

    def _upload_converted(_job_id, _workspace, _config):
        """If called, will increment, indicating failure to stop on cancellation."""
        upload_calls["count"] += 1

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _karakeep_id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bookmark: None)
    monkeypatch.setattr(orchestrator, "_prepare_conversion_input", _prepare_conversion_input)
    monkeypatch.setattr(orchestrator, "_run_conversion", _run_conversion)
    monkeypatch.setattr(orchestrator, "_upload_converted", _upload_converted)

    config = ConversionConfig(_env_file=None)
    orchestrator.process_job_supervised(job.id, config)

    assert upload_calls["count"] == 0
    db_session.refresh(job)
    assert job.status == ConversionJobStatus.CANCELLED


def test_poll_picks_retryable_job_after_delay(monkeypatch, db_session: Session) -> None:
    """Ensure FAILED_RETRYABLE jobs are eligible once the backoff window expires."""
    now = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(loop, "_utcnow", lambda: now)

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
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
        loop, "process_job_supervised", lambda job_id, _config, poll_interval_seconds=2.0: processed.append(job_id)
    )

    config = ConversionConfig(_env_file=None)
    assert loop.poll_and_process_jobs(config) is True
    assert processed == [job.id]

    db_session.refresh(job)
    assert job.status == ConversionJobStatus.RUNNING
    assert job.attempts == 2


def test_poll_skips_retryable_job_before_delay(monkeypatch, db_session: Session) -> None:
    """Ensure FAILED_RETRYABLE jobs are not picked up before earliest_next_attempt_at."""
    now = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(loop, "_utcnow", lambda: now)

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
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
        loop, "process_job_supervised", lambda job_id, _config, poll_interval_seconds=2.0: processed.append(job_id)
    )

    config = ConversionConfig(_env_file=None)
    assert loop.poll_and_process_jobs(config) is False
    assert processed == []


def test_raise_if_cancelled_raises(db_session: Session) -> None:
    """Raise a cancellation exception when a job is already cancelled."""
    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title=bookmark.title,
        idempotency_key="d" * 64,
        status=ConversionJobStatus.CANCELLED,
    )
    db_session.add(job)
    db_session.commit()

    with pytest.raises(errors_mod.ConversionCancelledError):
        orchestrator._raise_if_cancelled(job.id, db_session.get_bind())


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
        return queue_module.Queue()

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
        if timeout is not None and self._is_alive:
            time.sleep(timeout)
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
        return queue_module.Queue()

    def Process(self, target, args, daemon: bool) -> _TestProcess:  # noqa: N802
        return _TestProcess(
            target,
            args,
            daemon,
            alive_cycles=self._alive_cycles,
            exitcode=self._exitcode,
            on_start=self._on_start,
        )


def _create_running_job(db_session: Session, bookmark: Source) -> ConversionJob:
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
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

    # Short timeout; stub stays alive for 3 cycles (3 * 0.01s = 30ms > 0.01s timeout)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext(alive_cycles=3))
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _karakeep_id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bookmark: None)

    killpg_calls = []
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    errors: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=0.01)

    assert len(errors) == 1
    assert isinstance(errors[0], errors_mod.ConversionTimeoutError)
    assert len(killpg_calls) > 0, "Should call os.killpg() to terminate process group"


def test_process_job_supervised_cancels_child(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Cancellation polling terminates the child process."""
    import os

    fp.allow_unregistered(False)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)  # Immediately cancelled
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _karakeep_id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bookmark: None)

    # Track os.killpg calls
    killpg_calls = []

    def _track_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)
    monkeypatch.setattr(os, "killpg", _track_killpg)

    handle_calls: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: handle_calls.append(error))

    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=0.1)

    assert len(killpg_calls) > 0, "Should call os.killpg() to terminate process group on cancellation"
    assert handle_calls == []


def test_process_job_supervised_reports_subprocess_error(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Non-zero exit codes report a subprocess error."""
    fp.allow_unregistered(False)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext(exitcode=1))
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _karakeep_id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bookmark: None)

    errors: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=0.1)

    assert len(errors) == 1
    # When subprocess raises an error, it's reported as ReportedChildError
    assert isinstance(errors[0], (errors_mod.ReportedChildError, errors_mod.ConversionSubprocessError))


def test_timeout_during_subprocess_terminates_and_reports_phase(
    monkeypatch, db_session: Session, html_bookmark, fp
) -> None:
    """Deadline during polling terminates child and reports last phase."""
    import os

    fp.allow_unregistered(False)
    # Timeout 0.01s; stub stays alive 5 cycles (5 * 0.005s = 25ms > 10ms timeout)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "2")
    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    # Status queue reports converting immediately
    def _on_start(process_stub: _TestProcess) -> None:
        status_queue = process_stub._args[3]
        status_queue.put_nowait({"event": "phase", "message": "converting"})

    monkeypatch.setattr(
        orchestrator.mp, "get_context", lambda _ctx: _TestContext(alive_cycles=5, exitcode=0, on_start=_on_start)
    )
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)

    killpg_calls = []
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    errors: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=0.005)

    assert errors, "Timeout should be reported"
    assert isinstance(errors[0], errors_mod.ConversionTimeoutError)
    assert errors[0].phase == "converting"
    assert killpg_calls, "Process group should be terminated on timeout"


def test_timeout_before_upload_reports_uploading_phase(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Deadline exceeded before upload raises ConversionTimeoutError with uploading phase."""
    # Short timeout; supervision stub sleeps long enough for the deadline to expire
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "0.005")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "2")
    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    def _fake_supervise(**_kwargs):
        time.sleep(0.02)  # Exceed the 5ms deadline
        return SupervisionResult("converting", None, False, False)

    monkeypatch.setattr(orchestrator, "_supervise_conversion_process", _fake_supervise)

    upload_calls = {"count": 0}
    monkeypatch.setattr(
        orchestrator,
        "_upload_converted",
        lambda _job_id, _workspace, _config: upload_calls.__setitem__("count", upload_calls["count"] + 1),
    )

    errors: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    class _StubProcess:
        pid = 123
        exitcode = 0

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(
        orchestrator, "_spawn_conversion_subprocess", lambda **_kwargs: (_StubProcess(), queue_module.Queue())
    )

    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config)

    assert errors, "Timeout before upload should raise"
    assert isinstance(errors[0], errors_mod.ConversionTimeoutError)
    assert errors[0].phase == "uploading"
    assert upload_calls["count"] == 0


def test_timeout_during_upload_retry_stops_retrying(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Deadline during upload retries raises timeout and prevents further attempts."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "0.005")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "2")
    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(
        orchestrator,
        "_supervise_conversion_process",
        lambda **_kwargs: SupervisionResult("converting", None, False, False),
    )
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    upload_calls = {"count": 0}

    def _upload_converted_raises(_job_id, _workspace, _config):
        upload_calls["count"] += 1
        raise RuntimeError("upload failed")

    monkeypatch.setattr(orchestrator, "_upload_converted", _upload_converted_raises)

    errors: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    _real_sleep = time.sleep
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _delay: _real_sleep(0.01))

    class _StubProcess:
        pid = 123
        exitcode = 0

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(
        orchestrator, "_spawn_conversion_subprocess", lambda **_kwargs: (_StubProcess(), queue_module.Queue())
    )

    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config)

    # Upload attempted once, then timeout triggers after retry sleep
    assert upload_calls["count"] == 1
    assert errors, "Timeout should be reported during retry loop"
    assert isinstance(errors[0], errors_mod.ConversionTimeoutError)
    assert errors[0].phase == "uploading"


def test_timeout_logs_elapsed_with_phase(monkeypatch, db_session: Session, html_bookmark, caplog, fp) -> None:
    """Timeouts log job id, phase, and elapsed seconds."""
    import os

    caplog.set_level("INFO")
    fp.allow_unregistered(False)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "2")
    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)

    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext(alive_cycles=5, exitcode=0))
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: None)

    killpg_calls = []
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=0.005)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert f"Job {job.id}" in messages
    assert "timed out during" in messages
    assert "converting" in messages or "starting" in messages
    assert "after" in messages


def test_process_job_skips_spawn_for_cancelled_job(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Mark job CANCELLED before start; ensure no subprocess is spawned."""
    # Create a cancelled job
    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title=bookmark.title or "",
        idempotency_key="z" * 64,
        status=ConversionJobStatus.CANCELLED,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    # Track if a subprocess would be spawned
    spawned = {"count": 0}

    ctx = orchestrator.mp.get_context("spawn")

    def _track_process(target, args, daemon):
        spawned["count"] += 1
        return _InlineProcess(target, args)

    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)
    monkeypatch.setattr(ctx, "Process", _track_process)

    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config)

    # Since job was CANCELLED, we should return early without spawning
    assert spawned["count"] == 0


def test_logs_cancellation_during_phase(monkeypatch, db_session: Session, html_bookmark, caplog) -> None:
    """Verify log contains 'cancelled during {phase}' when cancelled in polling."""
    caplog.set_level("INFO")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    # Simulate a short-lived alive loop where cancellation is detected immediately
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext(alive_cycles=2, exitcode=0))
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    # Immediately report cancelled
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)

    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=0.05)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert f"Job {job.id} cancelled" in messages and "during" in messages


def test_cancelled_before_upload_skips_upload(monkeypatch, db_session: Session, html_bookmark, caplog) -> None:
    """Simulate cancellation after subprocess completion but before upload."""
    caplog.set_level("INFO")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    # Simulate successful conversion with no errors and normal exit
    monkeypatch.setattr(
        orchestrator,
        "_supervise_conversion_process",
        lambda **_kwargs: SupervisionResult("converting", None, False, False),
    )

    # Ensure we detect cancellation before upload phase
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)

    upload_calls = {"count": 0}

    def _upload_converted(_job_id, _workspace, _config):
        upload_calls["count"] += 1

    monkeypatch.setattr(orchestrator, "_upload_converted", _upload_converted)
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bm: None)

    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config)

    # Upload should be skipped and a log should indicate cancellation before upload
    assert upload_calls["count"] == 0
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert f"Job {job.id} cancelled before upload" in messages


# Process Group Management Tests


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
    monkeypatch.setattr(orchestrator, "_convert_job_artifacts", lambda **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_report_status", lambda *_args, **_kwargs: None)

    # Call the subprocess entrypoint directly
    from pathlib import Path

    test_queue = queue_module.Queue()
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = Path(tmpdir)
        payload_path = workspace_path / "payload.json"
        orchestrator._process_job_subprocess(
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
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext())
    killpg_calls = []

    def _mock_killpg(pgid, sig):
        killpg_calls.append({"pgid": pgid, "signal": sig})

    monkeypatch.setattr(os, "killpg", _mock_killpg)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)  # Trigger cancellation
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _karakeep_id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bookmark: None)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)

    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=0.1)

    # Should call killpg with SIGTERM first, then SIGKILL
    assert len(killpg_calls) >= 1, "Should call killpg at least once"
    # First call should be SIGTERM
    assert killpg_calls[0]["signal"] == signal.SIGTERM


def test_process_group_handles_esrch_gracefully(monkeypatch, db_session: Session, html_bookmark, fp) -> None:
    """Verify ProcessLookupError (ESRCH) is caught and doesn't propagate."""
    import os

    fp.allow_unregistered(False)
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext())

    def _mock_killpg(pgid, sig):
        raise ProcessLookupError("Process group already gone")

    monkeypatch.setattr(os, "killpg", _mock_killpg)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(orchestrator, "fetch_karakeep_bookmark", lambda _karakeep_id, **_kwargs: html_bookmark)
    monkeypatch.setattr(orchestrator, "validate_bookmark_content", lambda _bookmark: None)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)

    # Should not raise ProcessLookupError, should handle gracefully
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, poll_interval_seconds=0.1)
    # Test passes if no exception is raised


def test_handle_job_error_retryable_sets_failed_retryable(monkeypatch, db_session: Session) -> None:
    """Retryable exceptions should mark job FAILED_RETRYABLE with next attempt set."""
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "1")
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
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

    config = ConversionConfig(_env_file=None)
    orchestrator.handle_job_error(job.id, CustomRetryableError("boom"), config)

    db_session.refresh(job)
    assert job.status == ConversionJobStatus.FAILED_RETRYABLE
    assert job.earliest_next_attempt_at is not None
    assert job.finished_at is None


def test_handle_job_error_permanent_sets_failed_perm(monkeypatch, db_session: Session) -> None:
    """Permanent exceptions should mark job FAILED_PERM with finished_at set."""
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title=bookmark.title,
        idempotency_key="p" * 64,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    config = ConversionConfig(_env_file=None)
    orchestrator.handle_job_error(job.id, BookmarkContentError("missing content"), config)

    db_session.refresh(job)
    assert job.status == ConversionJobStatus.FAILED_PERM
    assert job.earliest_next_attempt_at is None
    assert job.finished_at is not None


def test_handle_job_error_missing_artifacts_is_permanent(monkeypatch, db_session: Session) -> None:
    """ConversionArtifactsMissingError is a permanent failure (retryable=False)."""
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())

    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title=bookmark.title,
        idempotency_key="q" * 64,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    config = ConversionConfig(_env_file=None)
    orchestrator.handle_job_error(job.id, errors_mod.ConversionArtifactsMissingError("no artifacts"), config)

    db_session.refresh(job)
    assert job.status == ConversionJobStatus.FAILED_PERM
    assert job.finished_at is not None


def test_error_code_and_retryable_mapping() -> None:
    """Every exception class carries an explicit retryable class attribute."""
    # Worker-defined errors — permanent
    jde = errors_mod.JobDataIntegrityError("bad job")
    assert jde.error_code == "job_data_integrity"
    assert jde.retryable is False

    ame = errors_mod.ConversionArtifactsMissingError("no output")
    assert ame.error_code == "conversion_artifacts_missing"
    assert ame.retryable is False

    cce = errors_mod.ConversionCancelledError("job cancelled")
    assert cce.error_code == "conversion_cancelled"
    assert cce.retryable is False

    # Worker-defined errors — retryable
    cto = errors_mod.ConversionTimeoutError("timeout", phase="converting")
    assert cto.error_code == "conversion_timeout"
    assert cto.retryable is True

    cse = errors_mod.ConversionSubprocessError("subprocess failed")
    assert cse.error_code == "conversion_subprocess_failed"
    assert cse.retryable is True

    pfe = errors_mod.PreflightError("preflight failed")
    assert pfe.error_code == "conversion_preflight_failed"
    assert pfe.retryable is True

    # ReportedChildError: class default is retryable; instance can override
    rce_default = errors_mod.ReportedChildError("child failed", "transient")
    assert rce_default.retryable is True  # class-level default
    rce_perm = errors_mod.ReportedChildError("child failed", "docling_empty_output", retryable=False)
    assert rce_perm.retryable is False  # instance override
    rce_retry = errors_mod.ReportedChildError("child failed", "transient", retryable=True)
    assert rce_retry.retryable is True

    # Bookmark content errors (permanent)
    bce = BookmarkContentError("missing")
    assert bce.error_code == "karakeep_bookmark_missing_contents"
    assert bce.retryable is False

    # Converter errors
    deo = converter.DoclingEmptyOutputError()
    assert deo.error_code == "docling_empty_output"
    assert deo.retryable is False

    # Fetcher errors
    fe = fetcher.FetchError("network")
    assert fe.error_code == "fetch_error"
    assert fe.retryable is True

    # S3 errors
    s3e = S3Error("bucket not configured", "s3_upload_failed")
    assert s3e.retryable is True
    s3_upload_err = S3UploadError("key/obj", "ETag mismatch")
    assert s3_upload_err.retryable is True


def _make_workspace_metadata(tmp_path: Path, *, markdown_hash: str) -> Path:
    """Write a minimal workspace with metadata.json and output.md."""
    (tmp_path / "output.md").write_text("# Content")
    metadata = {
        "markdown_filename": "output.md",
        "figure_files": [],
        "markdown_hash_xx64": markdown_hash,
        "docling_version": "1.0.0",
        "pipeline_name": "html",
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "config_snapshot": {
            "pdf_max_pages": 250,
            "ocr_enabled": True,
            "table_structure_enabled": True,
            "picture_description_model": "none",
            "picture_timeout": 60.0,
            "picture_classification_enabled": True,
            "picture_description_enabled": False,
        },
    }
    import json

    (tmp_path / "metadata.json").write_text(json.dumps(metadata))
    return tmp_path


def test_upload_converted_reuses_s3_when_hash_matches(monkeypatch, db_session: Session, tmp_path: Path) -> None:
    """When content hash matches a prior output, S3 upload is skipped and existing keys are reused."""
    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())

    bookmark = Source.from_karakeep_id(
        karakeep_id="bm_hash_reuse",
        url="https://example.com",
        normalized_url="https://example.com",
        title="Hash Reuse",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    prior_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title="Hash Reuse",
        idempotency_key="p" * 64,
        status=ConversionJobStatus.SUCCEEDED,
    )
    db_session.add(prior_job)
    db_session.commit()
    db_session.refresh(prior_job)

    known_hash = "abc123def456789a"
    prior_output = ConversionOutput(
        job_id=prior_job.id,
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title="Hash Reuse",
        payload_version=1,
        s3_prefix=f"s3://bucket/{bookmark.aizk_uuid}/",
        markdown_key=f"s3://bucket/{bookmark.aizk_uuid}/output.md",
        manifest_key=f"s3://bucket/{bookmark.aizk_uuid}/manifest.json",
        markdown_hash_xx64=known_hash,
        figure_count=0,
        docling_version="1.0.0",
        pipeline_name="html",
    )
    db_session.add(prior_output)
    db_session.commit()

    new_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title="Hash Reuse",
        idempotency_key="n" * 64,
        status=ConversionJobStatus.RUNNING,
    )
    db_session.add(new_job)
    db_session.commit()
    db_session.refresh(new_job)

    workspace = _make_workspace_metadata(tmp_path, markdown_hash=known_hash)

    upload_calls: list[str] = []

    class _MockS3Client:
        bucket = "bucket"

        def upload_file(self, local_path, s3_key):
            upload_calls.append(s3_key)
            return f"s3://bucket/{s3_key}"

    monkeypatch.setattr(uploader, "S3Client", lambda _config: _MockS3Client())

    config = ConversionConfig(_env_file=None)
    uploader._upload_converted(new_job.id, workspace, config)

    assert upload_calls == [], "S3 upload should be skipped when hash matches"

    db_session.refresh(new_job)
    assert new_job.status == ConversionJobStatus.SUCCEEDED

    from sqlmodel import select as _select

    outputs = db_session.exec(_select(ConversionOutput).where(ConversionOutput.job_id == new_job.id)).all()
    assert len(outputs) == 1
    assert outputs[0].markdown_key == prior_output.markdown_key
    assert outputs[0].s3_prefix == prior_output.s3_prefix
    assert outputs[0].markdown_hash_xx64 == known_hash


def test_upload_converted_uploads_when_hash_differs(monkeypatch, db_session: Session, tmp_path: Path) -> None:
    """When no prior output has a matching hash, the full S3 upload proceeds."""
    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())

    bookmark = Source.from_karakeep_id(
        karakeep_id="bm_hash_upload",
        url="https://example.com",
        normalized_url="https://example.com",
        title="Hash Upload",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    new_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title="Hash Upload",
        idempotency_key="u" * 64,
        status=ConversionJobStatus.RUNNING,
    )
    db_session.add(new_job)
    db_session.commit()
    db_session.refresh(new_job)

    workspace = _make_workspace_metadata(tmp_path, markdown_hash="newhash00000001a")

    upload_calls: list[str] = []

    class _MockS3Client:
        bucket = "test-bucket"

        def upload_file(self, local_path, s3_key):
            upload_calls.append(s3_key)
            return f"s3://test-bucket/{s3_key}"

    monkeypatch.setattr(uploader, "S3Client", lambda _config: _MockS3Client())

    config = ConversionConfig(_env_file=None)
    uploader._upload_converted(new_job.id, workspace, config)

    assert any("output.md" in key for key in upload_calls), "Markdown should be uploaded when no hash match"

    db_session.refresh(new_job)
    assert new_job.status == ConversionJobStatus.SUCCEEDED


def test_initialize_running_job_returns_false_for_cancelled_after_running_set(
    db_session: Session,
) -> None:
    """Job cancelled after poll sets RUNNING is detected before subprocess starts."""
    bookmark = _create_bookmark(db_session)
    # Simulate the state after poll_and_process_jobs committed RUNNING
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        source_ref=bookmark.source_ref,
        title=bookmark.title,
        idempotency_key="r" * 64,
        status=ConversionJobStatus.RUNNING,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    # API cancels the job (happens between poll commit and _initialize_running_job call)
    job.status = ConversionJobStatus.CANCELLED
    db_session.add(job)
    db_session.commit()

    result = orchestrator._initialize_running_job(job.id, db_session.get_bind())

    assert result is False, "Should not proceed when job is CANCELLED"
    db_session.refresh(job)
    assert job.status == ConversionJobStatus.CANCELLED, "Status must not be changed back to RUNNING"
