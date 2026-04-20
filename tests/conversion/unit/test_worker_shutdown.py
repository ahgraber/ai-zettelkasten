"""Unit tests for graceful shutdown integration in supervision and worker loop."""

from __future__ import annotations

import datetime as dt
import os
import signal
import threading
import time

from pyleak import no_thread_leaks
import pytest
from sqlmodel import Session

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as Bookmark
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import errors as errors_mod, loop, orchestrator, shutdown
from aizk.conversion.workers.supervision import _supervise_conversion_process
from aizk.conversion.workers.types import SupervisionResult


@pytest.fixture(autouse=True)
def _reset_shutdown():
    shutdown.reset()
    yield
    shutdown.reset()


def _create_bookmark(db_session: Session) -> Bookmark:
    _ref = KarakeepBookmarkRef(bookmark_id="bm_shutdown_test")
    bookmark = Bookmark(
        karakeep_id="bm_shutdown_test",
        source_ref=_ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(_ref),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Shutdown Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)
    return bookmark


def _create_running_job(db_session: Session, bookmark: Bookmark) -> ConversionJob:
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title or "",
        idempotency_key="s" * 64,
        status=ConversionJobStatus.RUNNING,
        started_at=dt.datetime.now(dt.timezone.utc),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


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


# ---------------------------------------------------------------------------
# Supervision: shutdown drain integration
# ---------------------------------------------------------------------------


class TestSupervisionShutdownDrain:
    """Tests for shutdown-aware subprocess supervision."""

    def test_shutdown_during_supervision_job_completes_within_drain(self, monkeypatch):
        """When shutdown is requested but the job finishes before drain timeout, no forced termination."""
        import queue as queue_module

        # Process completes after 2 join cycles (~10ms with 5ms poll interval)
        process = _StubProcess(alive_cycles=2)
        status_queue = queue_module.Queue()

        shutdown.request_shutdown()

        monkeypatch.setattr(os, "getpgrp", lambda: 111)

        result = _supervise_conversion_process(
            job_id=1,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=0.005,
            deadline=None,
            timeout_seconds=0,
            is_cancelled_fn=lambda: False,
            shutdown_requested_fn=shutdown.is_shutdown_requested,
            drain_timeout_seconds=5.0,  # Long drain — job finishes first
        )

        assert result.shutdown_terminated is False
        assert result.timed_out is False
        assert result.cancelled is False

    def test_drain_timeout_force_terminates_subprocess(self, monkeypatch):
        """When drain timeout expires, subprocess is force-terminated."""
        import queue as queue_module

        process = _StubProcess(alive_cycles=100)  # Stays alive long enough
        status_queue = queue_module.Queue()

        shutdown.request_shutdown()

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
            shutdown_requested_fn=shutdown.is_shutdown_requested,
            drain_timeout_seconds=0.01,
        )

        assert result.shutdown_terminated is True
        assert result.timed_out is False
        assert len(killpg_calls) >= 1
        assert killpg_calls[0][1] == signal.SIGTERM

    def test_no_shutdown_fn_means_no_drain(self):
        """Without shutdown_requested_fn, supervision runs normally even if flag is set."""
        import queue as queue_module

        process = _StubProcess(alive_cycles=1)
        status_queue = queue_module.Queue()

        shutdown.request_shutdown()  # Set flag, but no fn passed to supervision

        result = _supervise_conversion_process(
            job_id=1,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=0.005,
            deadline=None,
            timeout_seconds=0,
            is_cancelled_fn=lambda: False,
        )

        assert result.shutdown_terminated is False

    def test_job_timeout_takes_precedence_over_drain(self, monkeypatch):
        """Job timeout fires before drain timeout."""
        import queue as queue_module

        process = _StubProcess(alive_cycles=100)
        status_queue = queue_module.Queue()

        shutdown.request_shutdown()

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
            shutdown_requested_fn=shutdown.is_shutdown_requested,
            drain_timeout_seconds=300.0,
        )

        assert result.timed_out is True
        assert result.shutdown_terminated is False


# ---------------------------------------------------------------------------
# Orchestrator: shutdown_terminated handling
# ---------------------------------------------------------------------------


class TestOrchestratorShutdownTerminated:
    """Tests for orchestrator handling of shutdown-terminated supervision results."""

    def test_shutdown_terminated_calls_handle_job_error(self, monkeypatch, db_session, fp):
        """Shutdown termination transitions job to FAILED_RETRYABLE via handle_job_error."""

        fp.allow_unregistered(False)

        bookmark = _create_bookmark(db_session)
        job = _create_running_job(db_session, bookmark)

        # Set source_ref so _get_source_ref succeeds
        source_ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id="bm_shutdown_test")
        job.source_ref = source_ref.model_dump_json()
        db_session.add(job)
        db_session.commit()

        monkeypatch.setattr(
            orchestrator,
            "_supervise_conversion_process",
            lambda **_kwargs: SupervisionResult("converting", None, False, False, shutdown_terminated=True),
        )
        monkeypatch.setattr(orchestrator, "_is_job_cancelled", lambda _job_id, _engine: False)
        monkeypatch.setattr(orchestrator, "get_engine", lambda _database_url=None: db_session.get_bind())

        import queue as queue_module

        class _StubProc:
            pid = 123
            exitcode = 0

            def is_alive(self):
                return False

        monkeypatch.setattr(
            orchestrator, "_spawn_conversion_subprocess", lambda **_kwargs: (_StubProc(), queue_module.Queue())
        )

        errors: list[Exception] = []
        monkeypatch.setattr(orchestrator, "handle_job_error", lambda _job_id, error, _config: errors.append(error))

        from unittest.mock import MagicMock

        from aizk.conversion.wiring.worker import WorkerRuntime

        fake_caps = MagicMock()
        fake_caps.converter_requires_gpu.return_value = False
        fake_runtime = WorkerRuntime(
            orchestrator=MagicMock(),
            resource_guard=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)),
            capabilities=fake_caps,
        )

        config = ConversionConfig(_env_file=None)
        orchestrator.process_job_supervised(job.id, config, fake_runtime)

        assert len(errors) == 1
        assert isinstance(errors[0], errors_mod.ConversionTimeoutError)
        assert "shutdown drain" in str(errors[0]).lower()


# ---------------------------------------------------------------------------
# Worker loop: shutdown integration
# ---------------------------------------------------------------------------


class TestRunWorkerShutdown:
    """Tests for run_worker graceful shutdown behavior."""

    def _stub_build_runtime(self, monkeypatch):
        """Stub build_worker_runtime so tests don't build real adapters."""
        from unittest.mock import MagicMock

        from aizk.conversion.wiring.worker import WorkerRuntime

        fake_caps = MagicMock()
        fake_caps.converter_requires_gpu.return_value = False
        fake_runtime = WorkerRuntime(
            orchestrator=MagicMock(),
            resource_guard=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)),
            capabilities=fake_caps,
        )
        monkeypatch.setattr(loop, "build_worker_runtime", lambda _cfg: fake_runtime)

    def test_shutdown_while_idle_exits_zero(self, monkeypatch):
        """Signal during idle sleep exits with code 0."""
        shutdown.request_shutdown()

        monkeypatch.setattr(loop, "register_signal_handlers", lambda: None)
        self._stub_build_runtime(monkeypatch)

        config = ConversionConfig(_env_file=None)
        exit_code = loop.run_worker(config, poll_interval_seconds=0.01)

        assert exit_code == 0

    def test_shutdown_after_job_completes_exits_zero(self, monkeypatch):
        """Signal during job processing; job finishes; exits 0."""
        claim_count = {"n": 0}

        def _fake_claim(_config):
            claim_count["n"] += 1
            if claim_count["n"] == 1:
                return 1
            shutdown.request_shutdown()
            return None

        def _fake_process(_job_id, _config, _runtime=None, *, poll_interval_seconds=2.0):
            pass  # Job completes immediately.

        monkeypatch.setattr(loop, "claim_next_job", _fake_claim)
        monkeypatch.setattr(loop, "process_job_supervised", _fake_process)
        monkeypatch.setattr(loop, "register_signal_handlers", lambda: None)
        self._stub_build_runtime(monkeypatch)
        monkeypatch.setattr(loop, "recover_stale_running_jobs", lambda _config: 0)

        config = ConversionConfig(_env_file=None)
        exit_code = loop.run_worker(config, poll_interval_seconds=0.01)

        assert exit_code == 0

    def test_immediate_shutdown_calls_force_exit(self, monkeypatch):
        """Second signal (immediate shutdown) invokes the force_exit seam to bypass thread join."""
        claim_count = {"n": 0}

        def _fake_claim(_config):
            claim_count["n"] += 1
            if claim_count["n"] == 1:
                return 1
            return None

        def _fake_process(_job_id, _config, _runtime=None, *, poll_interval_seconds=2.0):
            shutdown._handle_signal(signal.SIGTERM, None)
            shutdown._handle_signal(signal.SIGTERM, None)

        exit_calls: list[int] = []
        monkeypatch.setattr(loop, "claim_next_job", _fake_claim)
        monkeypatch.setattr(loop, "process_job_supervised", _fake_process)
        monkeypatch.setattr(loop, "register_signal_handlers", lambda: None)
        self._stub_build_runtime(monkeypatch)
        monkeypatch.setattr(loop, "recover_stale_running_jobs", lambda _config: 0)
        monkeypatch.setattr(loop, "force_exit", lambda code: exit_calls.append(code))

        config = ConversionConfig(_env_file=None)
        loop.run_worker(config, poll_interval_seconds=0.01)

        assert exit_calls == [1]

    def test_forced_shutdown_with_real_executor_thread(self, monkeypatch):
        """Real ThreadPoolExecutor thread that outlives drain triggers force_exit(1).

        Unlike test_immediate_shutdown_calls_force_exit (which uses an instant
        fake), this test submits a blocking task to a real executor so a
        genuine non-daemon thread is alive when drain reports timeout.
        pyleak's no_thread_leaks verifies no thread survives the test.
        """
        stop = threading.Event()
        claim_count = {"n": 0}
        exit_calls: list[int] = []

        def _fake_claim(_config):
            claim_count["n"] += 1
            if claim_count["n"] == 1:
                return 1
            shutdown.request_shutdown()
            return None

        def _blocking_process(_job_id, _config, _runtime=None, *, poll_interval_seconds=2.0):
            stop.wait(timeout=30)  # Block until cleanup

        def _mock_exit(code: int) -> None:
            exit_calls.append(code)
            stop.set()  # Unblock the thread (simulates force_exit killing all threads)

        monkeypatch.setattr(loop, "claim_next_job", _fake_claim)
        monkeypatch.setattr(loop, "process_job_supervised", _blocking_process)
        monkeypatch.setattr(loop, "register_signal_handlers", lambda: None)
        self._stub_build_runtime(monkeypatch)
        monkeypatch.setattr(loop, "recover_stale_running_jobs", lambda _config: 0)
        # Mock _drain_in_flight to return True immediately — the real drain
        # has a 15-second buffer that would make this test unacceptably slow.
        # Drain behaviour itself is tested in TestDrainInFlight.
        monkeypatch.setattr(loop, "_drain_in_flight", lambda futures, config: bool(futures))
        monkeypatch.setattr(loop, "force_exit", _mock_exit)

        config = ConversionConfig(_env_file=None)

        with no_thread_leaks(action="raise", grace_period=0.5):
            loop.run_worker(config, poll_interval_seconds=0.01)

        assert exit_calls == [1]

    def test_no_running_jobs_after_shutdown(self, monkeypatch, db_session):
        """After shutdown, no jobs should be in RUNNING state."""
        bookmark = _create_bookmark(db_session)
        job = _create_running_job(db_session, bookmark)

        claim_count = {"n": 0}

        def _fake_claim(_config):
            claim_count["n"] += 1
            if claim_count["n"] == 1:
                return job.id
            shutdown.request_shutdown()
            return None

        def _fake_process(_job_id, _config, _runtime=None, *, poll_interval_seconds=2.0):
            with Session(db_session.get_bind()) as session:
                j = session.get(ConversionJob, _job_id)
                j.status = ConversionJobStatus.FAILED_RETRYABLE
                session.add(j)
                session.commit()

        monkeypatch.setattr(loop, "claim_next_job", _fake_claim)
        monkeypatch.setattr(loop, "process_job_supervised", _fake_process)
        monkeypatch.setattr(loop, "register_signal_handlers", lambda: None)
        self._stub_build_runtime(monkeypatch)
        monkeypatch.setattr(loop, "recover_stale_running_jobs", lambda _config: 0)

        config = ConversionConfig(_env_file=None)
        loop.run_worker(config, poll_interval_seconds=0.01)

        db_session.refresh(job)
        assert job.status != ConversionJobStatus.RUNNING
