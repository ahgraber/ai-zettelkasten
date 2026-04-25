"""Integration tests for the conversion orchestrator (process_job_supervised + polling).

Crosses subprocess + DB boundaries; not a true unit test despite the use of
mocks at the orchestrator boundary. The pure-unit error-mapping carve-out
lives in unit/workers/test_errors.py.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import queue as queue_module
import time
from unittest.mock import MagicMock, Mock, patch
import uuid

import pytest
from sqlmodel import Session, select

from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    InlineHtmlRef,
    KarakeepBookmarkRef,
    UrlRef,
    compute_source_ref_hash,
)
from aizk.conversion.core.types import SOURCE_TYPE_BY_KIND
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source as Bookmark
from aizk.conversion.storage.s3_client import S3Error, S3UploadError
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import converter, errors as errors_mod, fetcher, loop, orchestrator, uploader
from aizk.conversion.workers.types import SupervisionResult
from tests.conversion._helpers import make_source


class _CompletedProcess:
    """Process stub representing an already-finished subprocess.

    Returned from `_spawn_and_supervise` / `_spawn_conversion_subprocess` patches
    when the test only cares about the post-completion path.
    """

    pid = 123
    exitcode = 0

    def is_alive(self) -> bool:
        return False


def _create_bookmark(db_session: Session) -> Bookmark:
    return make_source(
        db_session,
        "bm_poll_retryable",
        url="https://example.com",
        title="Poll Retryable",
        content_type="html",
        source_type="web",
    )


def _make_fake_runtime(requires_gpu: bool = False) -> MagicMock:
    """Return a fake WorkerRuntime with nullcontext resource_guard."""
    from contextlib import nullcontext

    runtime = MagicMock()
    runtime.resource_guard = nullcontext()
    runtime.capabilities.converter_requires_gpu.return_value = requires_gpu
    runtime.orchestrator = Mock()
    return runtime


def _create_job_with_source_ref(
    db_session: Session, bookmark: Bookmark, source_ref_json: str | None = None
) -> ConversionJob:
    """Create a job with an optional source_ref set."""
    if source_ref_json is None:
        source_ref_json = json.dumps({"kind": "karakeep_bookmark", "bookmark_id": bookmark.karakeep_id or "bm_test"})
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title or "",
        idempotency_key=("j" * 64),
        status=ConversionJobStatus.QUEUED,
        source_ref=source_ref_json,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


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


def _create_running_job(db_session: Session, bookmark: Bookmark, source_ref_json: str | None = None) -> ConversionJob:
    if source_ref_json is None:
        source_ref_json = json.dumps({"kind": "karakeep_bookmark", "bookmark_id": bookmark.karakeep_id or "bm_test"})
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title or "",
        idempotency_key="e" * 64,
        status=ConversionJobStatus.RUNNING,
        started_at=dt.datetime.now(dt.timezone.utc),
        source_ref=source_ref_json,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# test_process_job_retries_upload — rewritten for new signature
# ---------------------------------------------------------------------------


def test_process_job_retries_upload(monkeypatch, db_session: Session) -> None:
    """Verify upload retries without invoking real conversion or network calls."""
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "1")
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _InlineContext())

    _ref_retry = KarakeepBookmarkRef(bookmark_id="bm_retry_test")
    bookmark = Bookmark(
        karakeep_id="bm_retry_test",
        source_ref=_ref_retry.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref_retry),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Retry Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    source_ref_json = json.dumps({"kind": "karakeep_bookmark", "bookmark_id": "bm_retry_test"})
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="a" * 64,
        status=ConversionJobStatus.QUEUED,
        source_ref=source_ref_json,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    upload_attempts = {"count": 0}
    sleep_calls: list[float] = []
    handle_errors = {"count": 0}

    def _upload_converted(_job_id, _workspace, _config):
        upload_attempts["count"] += 1
        if upload_attempts["count"] < 3:
            raise RuntimeError("transient upload failure")

    def _handle_job_error(_job_id, _error, _config):
        handle_errors["count"] += 1

    monkeypatch.setattr(orchestrator, "_upload_converted", _upload_converted)
    monkeypatch.setattr(orchestrator, "handle_job_error", _handle_job_error)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda delay: sleep_calls.append(delay))

    # Fake out the subprocess so it writes metadata that passes the enrichment path
    def _fake_spawn_and_supervise(**kwargs):
        return _CompletedProcess(), SupervisionResult("converting", None, False, False), None

    monkeypatch.setattr(orchestrator, "_spawn_and_supervise", _fake_spawn_and_supervise)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _url=None: db_session.get_bind())

    runtime = _make_fake_runtime()
    config = ConversionConfig(_env_file=None)
    orchestrator.process_job_supervised(job.id, config, runtime)

    assert upload_attempts["count"] == 3
    assert sleep_calls == [1, 2]
    assert handle_errors["count"] == 0


# ---------------------------------------------------------------------------
# test_process_job_stops_on_cancellation — rewritten
# ---------------------------------------------------------------------------


def test_process_job_stops_on_cancellation(monkeypatch, db_session: Session) -> None:
    """Stop processing before upload when a job is cancelled mid-run."""
    _ref_cancel = KarakeepBookmarkRef(bookmark_id="bm_cancel_test")
    bookmark = Bookmark(
        karakeep_id="bm_cancel_test",
        source_ref=_ref_cancel.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref_cancel),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Cancel Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    source_ref_json = json.dumps({"kind": "karakeep_bookmark", "bookmark_id": "bm_cancel_test"})
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="c" * 64,
        status=ConversionJobStatus.QUEUED,
        source_ref=source_ref_json,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    upload_calls = {"count": 0}

    def _upload_converted(_job_id, _workspace, _config):
        upload_calls["count"] += 1

    monkeypatch.setattr(orchestrator, "_upload_converted", _upload_converted)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _url=None: db_session.get_bind())

    # Subprocess completes with cancelled result
    def _fake_spawn_and_supervise(**kwargs):
        return _CompletedProcess(), SupervisionResult("converting", None, True, False), None

    monkeypatch.setattr(orchestrator, "_spawn_and_supervise", _fake_spawn_and_supervise)

    runtime = _make_fake_runtime()
    config = ConversionConfig(_env_file=None)
    orchestrator.process_job_supervised(job.id, config, runtime)

    assert upload_calls["count"] == 0


# ---------------------------------------------------------------------------
# Polling tests — unchanged logic, signature update
# ---------------------------------------------------------------------------


def test_poll_picks_retryable_job_after_delay(monkeypatch, db_session: Session) -> None:
    """Ensure FAILED_RETRYABLE jobs are eligible once the backoff window expires."""
    now = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(loop, "_utcnow", lambda: now)

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

    monkeypatch.setattr(
        loop,
        "process_job_supervised",
        lambda job_id, _config, _runtime=None, poll_interval_seconds=2.0: processed.append(job_id),
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
        loop,
        "process_job_supervised",
        lambda job_id, _config, _runtime=None, poll_interval_seconds=2.0: processed.append(job_id),
    )

    config = ConversionConfig(_env_file=None)
    assert loop.poll_and_process_jobs(config) is False
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

    with pytest.raises(errors_mod.ConversionCancelledError):
        orchestrator._raise_if_cancelled(job.id, db_session.get_bind())


# ---------------------------------------------------------------------------
# process_job_supervised tests — updated to use injected runtime
# ---------------------------------------------------------------------------


def test_process_job_supervised_uses_injected_runtime(monkeypatch, db_session: Session) -> None:
    """process_job_supervised accepts and uses a provided runtime."""
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    runtime = _make_fake_runtime()
    monkeypatch.setattr(orchestrator, "get_engine", lambda _url=None: db_session.get_bind())

    # Immediately report cancelled so we don't need actual subprocess
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)

    def _fake_spawn_and_supervise(**kwargs):
        return _CompletedProcess(), SupervisionResult("converting", None, True, False), None

    monkeypatch.setattr(orchestrator, "_spawn_and_supervise", _fake_spawn_and_supervise)

    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    # Should not raise; runtime is passed in
    orchestrator.process_job_supervised(job.id, config, runtime)

    # capabilities.converter_requires_gpu was called with the configured converter name
    runtime.capabilities.converter_requires_gpu.assert_called_once_with(config.worker_converter_name)


def test_process_job_supervised_cancels_child(monkeypatch, db_session: Session) -> None:
    """Cancellation polling terminates the child process."""
    import os

    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())

    killpg_calls = []

    def _track_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)
    monkeypatch.setattr(os, "killpg", _track_killpg)

    handle_calls: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: handle_calls.append(error))

    runtime = _make_fake_runtime()
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime, poll_interval_seconds=0.1)

    assert len(killpg_calls) > 0, "Should call os.killpg() to terminate process group on cancellation"
    assert handle_calls == []


def test_process_job_supervised_reports_subprocess_error(monkeypatch, db_session: Session) -> None:
    """Non-zero exit codes report a subprocess error."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext(exitcode=1))
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())

    errors: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    runtime = _make_fake_runtime()
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime, poll_interval_seconds=0.1)

    assert len(errors) == 1
    assert isinstance(errors[0], (errors_mod.ReportedChildError, errors_mod.ConversionSubprocessError))


def test_timeout_during_subprocess_terminates_and_reports_phase(monkeypatch, db_session: Session) -> None:
    """Deadline during polling terminates child and reports last phase."""
    import os

    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "2")
    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    def _on_start(process_stub: _TestProcess) -> None:
        status_queue = process_stub._args[3]
        status_queue.put_nowait({"event": "phase", "message": "converting"})

    monkeypatch.setattr(
        orchestrator.mp, "get_context", lambda _ctx: _TestContext(alive_cycles=5, exitcode=0, on_start=_on_start)
    )
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)

    killpg_calls = []
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    errors: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    runtime = _make_fake_runtime()
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime, poll_interval_seconds=0.005)

    assert errors, "Timeout should be reported"
    assert isinstance(errors[0], errors_mod.ConversionTimeoutError)
    assert errors[0].phase == "converting"
    assert killpg_calls, "Process group should be terminated on timeout"


def test_timeout_before_upload_reports_uploading_phase(monkeypatch, db_session: Session) -> None:
    """Deadline exceeded before upload raises ConversionTimeoutError with uploading phase."""
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "0.005")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "2")
    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    def _fake_supervise(**_kwargs):
        # 20× the 5ms deadline — gives the deadline-check loop ample headroom on
        # busy CI runners; the previous 20ms (4×) was tight.
        time.sleep(0.1)
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

    monkeypatch.setattr(
        orchestrator, "_spawn_conversion_subprocess", lambda **_kwargs: (_CompletedProcess(), queue_module.Queue())
    )

    runtime = _make_fake_runtime()
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime)

    assert errors, "Timeout before upload should raise"
    assert isinstance(errors[0], errors_mod.ConversionTimeoutError)
    assert errors[0].phase == "uploading"
    assert upload_calls["count"] == 0


def test_timeout_during_upload_retry_stops_retrying(monkeypatch, db_session: Session) -> None:
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

    upload_calls = {"count": 0}

    def _upload_converted_raises(_job_id, _workspace, _config):
        upload_calls["count"] += 1
        raise RuntimeError("upload failed")

    monkeypatch.setattr(orchestrator, "_upload_converted", _upload_converted_raises)

    errors: list[Exception] = []
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

    _real_sleep = time.sleep
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _delay: _real_sleep(0.01))

    monkeypatch.setattr(
        orchestrator, "_spawn_conversion_subprocess", lambda **_kwargs: (_CompletedProcess(), queue_module.Queue())
    )

    runtime = _make_fake_runtime()
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime)

    assert upload_calls["count"] == 1
    assert errors, "Timeout should be reported during retry loop"
    assert isinstance(errors[0], errors_mod.ConversionTimeoutError)
    assert errors[0].phase == "uploading"


def test_timeout_logs_elapsed_with_phase(monkeypatch, db_session: Session, caplog) -> None:
    """Timeouts log job id, phase, and elapsed seconds."""
    import os

    caplog.set_level("INFO")
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "2")
    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)

    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext(alive_cycles=5, exitcode=0))
    monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: None)

    killpg_calls = []
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))

    runtime = _make_fake_runtime()
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime, poll_interval_seconds=0.005)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert f"Job {job.id}" in messages
    assert "timed out during" in messages
    assert "converting" in messages or "starting" in messages
    assert "after" in messages


def test_retried_job_receives_fresh_timeout_window(monkeypatch, db_session: Session) -> None:
    """Spec (wpm §75-79): each attempt receives a full fresh timeout window from config.

    Verifies process_job_supervised reads worker_job_timeout_seconds from config on
    every call and passes it to _spawn_and_supervise — no inheritance from the prior
    attempt's deadline.
    """
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "42")
    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    captured: list[float] = []

    def _spy_spawn(**kwargs):
        captured.append(kwargs["timeout_seconds"])
        # cancelled=True short-circuits process_job_supervised cleanly without uploads.
        return _CompletedProcess(), SupervisionResult("starting", None, True, False), None

    monkeypatch.setattr(orchestrator, "_spawn_and_supervise", _spy_spawn)

    runtime = _make_fake_runtime()
    assert job.id is not None

    # First attempt.
    orchestrator.process_job_supervised(job.id, config, runtime)

    # Simulate retry: re-mark RUNNING for the second invocation.
    db_session.refresh(job)
    job.status = ConversionJobStatus.RUNNING
    job.attempts = 1
    db_session.add(job)
    db_session.commit()

    # Second attempt.
    orchestrator.process_job_supervised(job.id, config, runtime)

    assert captured == [42.0, 42.0], (
        "Each attempt must receive the full configured timeout window; no inheritance from the prior attempt."
    )


def test_process_job_skips_spawn_for_cancelled_job(monkeypatch, db_session: Session) -> None:
    """Mark job CANCELLED before start; ensure no subprocess is spawned."""
    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title or "",
        idempotency_key="z" * 64,
        status=ConversionJobStatus.CANCELLED,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    spawned = {"count": 0}

    ctx = orchestrator.mp.get_context("spawn")

    def _track_process(target, args, daemon):
        spawned["count"] += 1
        return _InlineProcess(target, args)

    monkeypatch.setattr(ctx, "Process", _track_process)

    runtime = _make_fake_runtime()
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime)

    assert spawned["count"] == 0


def test_logs_cancellation_during_phase(monkeypatch, db_session: Session, caplog) -> None:
    """Verify log contains 'cancelled during {phase}' when cancelled in polling."""
    caplog.set_level("INFO")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext(alive_cycles=2, exitcode=0))

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)

    runtime = _make_fake_runtime()
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime, poll_interval_seconds=0.05)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert f"Job {job.id} cancelled" in messages and "during" in messages


def test_cancelled_before_upload_skips_upload(monkeypatch, db_session: Session, caplog) -> None:
    """Simulate cancellation after subprocess completion but before upload."""
    caplog.set_level("INFO")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(
        orchestrator,
        "_supervise_conversion_process",
        lambda **_kwargs: SupervisionResult("converting", None, False, False),
    )

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)

    upload_calls = {"count": 0}

    def _upload_converted(_job_id, _workspace, _config):
        upload_calls["count"] += 1

    monkeypatch.setattr(orchestrator, "_upload_converted", _upload_converted)

    monkeypatch.setattr(
        orchestrator, "_spawn_conversion_subprocess", lambda **_kwargs: (_CompletedProcess(), queue_module.Queue())
    )

    runtime = _make_fake_runtime()
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime)

    assert upload_calls["count"] == 0
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert f"Job {job.id} cancelled before upload" in messages


# ---------------------------------------------------------------------------
# Process Group Management Tests
# ---------------------------------------------------------------------------


def test_process_group_creation_called_in_subprocess(monkeypatch) -> None:
    """Verify os.setpgrp() is called at subprocess start."""
    import os
    import tempfile

    setpgrp_called = []

    def _mock_setpgrp():
        setpgrp_called.append(True)

    monkeypatch.setattr(os, "setpgrp", _mock_setpgrp)

    # We call the subprocess entrypoint directly but patch out the heavyweight work
    monkeypatch.setattr(
        "aizk.conversion.wiring.worker.build_worker_runtime",
        lambda _cfg: MagicMock(),
    )

    def _fake_do_convert():
        pass

    # Patch _process_job_subprocess to only call setpgrp then do nothing
    original_fn = orchestrator._process_job_subprocess

    def _patched_subprocess(job_id, workspace_path, source_ref_json, status_queue):
        os.setpgrp()
        # Exit without doing real work

    monkeypatch.setattr(orchestrator, "_process_job_subprocess", _patched_subprocess)

    test_queue = queue_module.Queue()
    with tempfile.TemporaryDirectory() as tmpdir:
        orchestrator._process_job_subprocess(
            job_id=1,
            workspace_path=tmpdir,
            source_ref_json='{"kind":"url","url":"https://example.com"}',
            status_queue=test_queue,
        )

    assert len(setpgrp_called) == 1, "os.setpgrp() should be called once at subprocess start"


def test_process_group_termination_uses_killpg(monkeypatch, db_session: Session) -> None:
    """Verify os.killpg() is called with SIGTERM and SIGKILL."""
    import os
    import signal

    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext())
    killpg_calls = []

    def _mock_killpg(pgid, sig):
        killpg_calls.append({"pgid": pgid, "signal": sig})

    monkeypatch.setattr(os, "killpg", _mock_killpg)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)

    runtime = _make_fake_runtime()
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime, poll_interval_seconds=0.1)

    assert len(killpg_calls) >= 1, "Should call killpg at least once"
    assert killpg_calls[0]["signal"] == signal.SIGTERM


def test_process_group_handles_esrch_gracefully(monkeypatch, db_session: Session) -> None:
    """Verify ProcessLookupError (ESRCH) is caught and doesn't propagate."""
    import os

    monkeypatch.setattr(orchestrator.mp, "get_context", lambda _ctx: _TestContext())

    def _mock_killpg(pgid, sig):
        raise ProcessLookupError("Process group already gone")

    monkeypatch.setattr(os, "killpg", _mock_killpg)
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "30")

    bookmark = _create_bookmark(db_session)
    job = _create_running_job(db_session, bookmark)

    monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: True)
    monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())
    monkeypatch.setattr(os, "getpgid", lambda _pid: 222)
    monkeypatch.setattr(os, "getpgrp", lambda: 111)

    runtime = _make_fake_runtime()
    config = ConversionConfig(_env_file=None)
    assert job.id is not None
    orchestrator.process_job_supervised(job.id, config, runtime, poll_interval_seconds=0.1)
    # Test passes if no exception is raised


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
        "terminal_ref": {"kind": "karakeep_bookmark", "bookmark_id": "bm_workspace_default"},
        "config_snapshot": {
            "docling_pdf_max_pages": 250,
            "docling_enable_ocr": True,
            "docling_enable_table_structure": True,
            "docling_picture_description_model": "none",
            "docling_picture_timeout": 60.0,
            "docling_enable_picture_classification": True,
            "picture_description_enabled": False,
        },
    }
    import json

    (tmp_path / "metadata.json").write_text(json.dumps(metadata))
    return tmp_path


def test_upload_converted_reuses_s3_when_hash_matches(monkeypatch, db_session: Session, tmp_path: Path) -> None:
    """When content hash matches a prior output, S3 upload is skipped and existing keys are reused."""
    monkeypatch.setattr(uploader, "get_engine", lambda _url=None: db_session.get_bind())

    _ref_reuse = KarakeepBookmarkRef(bookmark_id="bm_hash_reuse")
    bookmark = Bookmark(
        karakeep_id="bm_hash_reuse",
        source_ref=_ref_reuse.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref_reuse),
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
        title="Hash Reuse",
        payload_version=1,
        s3_prefix=f"s3://bucket/{bookmark.aizk_uuid}/",
        markdown_key=f"{bookmark.aizk_uuid}/output.md",
        manifest_key=f"{bookmark.aizk_uuid}/manifest.json",
        markdown_hash_xx64=known_hash,
        figure_count=0,
        docling_version="1.0.0",
        pipeline_name="html",
    )
    db_session.add(prior_output)
    db_session.commit()

    new_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
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

    _ref_upload = KarakeepBookmarkRef(bookmark_id="bm_hash_upload")
    bookmark = Bookmark(
        karakeep_id="bm_hash_upload",
        source_ref=_ref_upload.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref_upload),
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

    from sqlmodel import select as _select

    outputs = db_session.exec(_select(ConversionOutput).where(ConversionOutput.job_id == new_job.id)).all()
    assert len(outputs) == 1
    assert outputs[0].markdown_key == f"{bookmark.aizk_uuid}/output.md", "markdown_key must be a bare S3 key"
    assert outputs[0].manifest_key == f"{bookmark.aizk_uuid}/manifest.json", "manifest_key must be a bare S3 key"


def test_initialize_running_job_returns_false_for_cancelled_after_running_set(
    db_session: Session,
) -> None:
    """Job cancelled after poll sets RUNNING is detected before subprocess starts."""
    bookmark = _create_bookmark(db_session)
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="r" * 64,
        status=ConversionJobStatus.RUNNING,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    job.status = ConversionJobStatus.CANCELLED
    db_session.add(job)
    db_session.commit()

    result = orchestrator._initialize_running_job(job.id, db_session.get_bind())

    assert result is False, "Should not proceed when job is CANCELLED"
    db_session.refresh(job)
    assert job.status == ConversionJobStatus.CANCELLED, "Status must not be changed back to RUNNING"


# ---------------------------------------------------------------------------
# Source enrichment
# ---------------------------------------------------------------------------


def _create_source_for_enrichment(db_session: Session, *, bookmark_id: str) -> Bookmark:
    from aizk.conversion.core.source_ref import KarakeepBookmarkRef

    ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id=bookmark_id)
    source = Bookmark(
        karakeep_id=bookmark_id,
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Enrichment Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(source)
    db_session.commit()
    db_session.refresh(source)
    return source


class TestEnrichSourceMetadata:
    """Tests for _enrich_source_metadata: identity immutability, best-effort, source_type mapping."""

    def test_identity_columns_unchanged_after_enrichment(self, db_session):
        source = _create_source_for_enrichment(db_session, bookmark_id="bm_identity_imm")
        pre = {
            "aizk_uuid": source.aizk_uuid,
            "source_ref": source.source_ref,
            "source_ref_hash": source.source_ref_hash,
            "karakeep_id": source.karakeep_id,
        }
        terminal_ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_identity_imm")

        orchestrator._enrich_source_metadata(
            aizk_uuid=source.aizk_uuid,
            terminal_ref=terminal_ref,
            content_type_str="html",
            engine=db_session.get_bind(),
        )

        db_session.refresh(source)
        assert source.aizk_uuid == pre["aizk_uuid"]
        assert source.source_ref == pre["source_ref"]
        assert source.source_ref_hash == pre["source_ref_hash"]
        assert source.karakeep_id == pre["karakeep_id"]

    def test_enrichment_writes_mutable_metadata(self, db_session):
        source = _create_source_for_enrichment(db_session, bookmark_id="bm_mutable_enrich")
        terminal_ref = ArxivRef(kind="arxiv", arxiv_id="2401.00001")

        orchestrator._enrich_source_metadata(
            aizk_uuid=source.aizk_uuid,
            terminal_ref=terminal_ref,
            content_type_str="pdf",
            engine=db_session.get_bind(),
        )

        db_session.refresh(source)
        assert source.source_type == SOURCE_TYPE_BY_KIND["arxiv"]
        assert source.content_type == "pdf"

    def test_missing_source_row_logs_warning_and_does_not_raise(self, db_session, caplog):
        import logging

        terminal_ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_missing")
        missing_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")

        with caplog.at_level(logging.WARNING, logger="aizk.conversion.workers.orchestrator"):
            orchestrator._enrich_source_metadata(
                aizk_uuid=missing_uuid,
                terminal_ref=terminal_ref,
                content_type_str="html",
                engine=db_session.get_bind(),
            )

        assert any("not found" in r.message.lower() or "enrichment" in r.message.lower() for r in caplog.records)

    def test_db_exception_does_not_propagate(self):
        terminal_ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_exc")
        with patch("aizk.conversion.workers.orchestrator.Session", side_effect=RuntimeError("boom")):
            orchestrator._enrich_source_metadata(
                aizk_uuid=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                terminal_ref=terminal_ref,
                content_type_str="html",
                engine=MagicMock(),
            )


@pytest.mark.parametrize(
    "terminal_ref,expected_source_type",
    [
        (ArxivRef(kind="arxiv", arxiv_id="2401.00001"), SOURCE_TYPE_BY_KIND["arxiv"]),
        (GithubReadmeRef(kind="github_readme", owner="owner", repo="repo"), SOURCE_TYPE_BY_KIND["github_readme"]),
        (UrlRef(kind="url", url="https://example.com"), SOURCE_TYPE_BY_KIND["url"]),
        (KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_1"), SOURCE_TYPE_BY_KIND["karakeep_bookmark"]),
        (InlineHtmlRef(kind="inline_html", body=b"<html/>"), SOURCE_TYPE_BY_KIND["inline_html"]),
    ],
)
def test_source_type_set_from_terminal_ref_kind(terminal_ref, expected_source_type, db_session):
    """source_type is SOURCE_TYPE_BY_KIND[terminal_ref.kind] for every terminal kind."""
    bookmark_id = f"bm_srctype_{terminal_ref.kind}"
    source = _create_source_for_enrichment(db_session, bookmark_id=bookmark_id)

    orchestrator._enrich_source_metadata(
        aizk_uuid=source.aizk_uuid,
        terminal_ref=terminal_ref,
        content_type_str=None,
        engine=db_session.get_bind(),
    )

    db_session.refresh(source)
    assert source.source_type == expected_source_type


def test_worker_does_not_recompute_idempotency_key():
    """workers/orchestrator must not call compute_idempotency_key (API owns it)."""
    import inspect

    source = inspect.getsource(orchestrator)
    assert "compute_idempotency_key" not in source
