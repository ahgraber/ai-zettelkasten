"""Unit tests for concurrent job processing in the worker loop."""

from __future__ import annotations

from concurrent.futures import Future
import datetime as dt
import threading
import time
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source as Bookmark
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import loop, shutdown


@pytest.fixture(autouse=True)
def _reset_shutdown():
    shutdown.reset()
    yield
    shutdown.reset()


def _create_bookmark(db_session: Session) -> Bookmark:
    bookmark = Bookmark(
        karakeep_id="bm_concurrency_test",
        url="https://example.com",
        normalized_url="https://example.com",
        title="Concurrency Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)
    return bookmark


def _create_queued_job(db_session: Session, bookmark: Bookmark, *, suffix: str = "") -> ConversionJob:
    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title or "",
        idempotency_key=("q" + suffix).ljust(64, "0"),
        status=ConversionJobStatus.QUEUED,
        queued_at=dt.datetime.now(dt.timezone.utc),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def _make_fake_runtime() -> MagicMock:
    """Return a fake WorkerRuntime with nullcontext resource_guard."""
    from contextlib import nullcontext

    runtime = MagicMock()
    runtime.resource_guard = nullcontext()
    runtime.capabilities.converter_requires_gpu.return_value = False
    runtime.orchestrator = MagicMock()
    return runtime


# ---------------------------------------------------------------------------
# claim_next_job
# ---------------------------------------------------------------------------


class TestClaimNextJob:
    def test_returns_job_id_for_queued_job(self, db_session, monkeypatch):
        bookmark = _create_bookmark(db_session)
        job = _create_queued_job(db_session, bookmark)
        monkeypatch.setattr(loop, "get_engine", lambda _url=None: db_session.get_bind())

        config = ConversionConfig(_env_file=None)
        result = loop.claim_next_job(config)

        assert result == job.id
        db_session.refresh(job)
        assert job.status == ConversionJobStatus.RUNNING

    def test_returns_none_when_no_jobs(self, db_session, monkeypatch):
        monkeypatch.setattr(loop, "get_engine", lambda _url=None: db_session.get_bind())

        config = ConversionConfig(_env_file=None)
        result = loop.claim_next_job(config)

        assert result is None


# ---------------------------------------------------------------------------
# _reap_completed
# ---------------------------------------------------------------------------


class TestReapCompleted:
    def test_removes_done_futures(self):
        f1 = Future()
        f1.set_result(None)
        f2 = Future()  # not done

        futures = {f1: 1, f2: 2}
        loop._reap_completed(futures)

        assert f1 not in futures
        assert f2 in futures

    def test_logs_unexpected_exceptions(self, caplog):
        f = Future()
        f.set_exception(RuntimeError("boom"))

        futures = {f: 42}
        with caplog.at_level("ERROR"):
            loop._reap_completed(futures)

        assert f not in futures
        assert "Job 42" in caplog.text
        assert "boom" in caplog.text


# ---------------------------------------------------------------------------
# GPU guard (via _SemaphoreGuard in wiring.worker)
# ---------------------------------------------------------------------------


class TestGpuSemaphoreGuard:
    def test_semaphore_guard_limits_concurrent_access(self):
        """Verify only N threads can hold the semaphore simultaneously."""
        from aizk.conversion.wiring.worker import _SemaphoreGuard

        sem = threading.Semaphore(1)
        guard = _SemaphoreGuard(sem)

        max_concurrent = {"value": 0}
        current = {"value": 0}
        lock = threading.Lock()

        def _worker():
            with guard:
                with lock:
                    current["value"] += 1
                    max_concurrent["value"] = max(max_concurrent["value"], current["value"])
                time.sleep(0.01)
                with lock:
                    current["value"] -= 1

        threads = [threading.Thread(target=_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert max_concurrent["value"] == 1

    def test_requires_gpu_false_does_not_acquire_guard(self, monkeypatch):
        """_spawn_and_supervise with requires_gpu=False must not enter the guard."""
        from pathlib import Path
        import queue as queue_module

        from aizk.conversion.workers import orchestrator as orchestrator_mod

        acquire_calls: list[str] = []

        class _TrackingGuard:
            def __enter__(self):
                acquire_calls.append("enter")
                return self

            def __exit__(self, *_):
                acquire_calls.append("exit")

        class _StubProcess:
            pid = None
            exitcode = 0

            def start(self):
                pass

            def is_alive(self):
                return False

            def join(self, timeout=None):
                pass

            def terminate(self):
                pass

            def kill(self):
                pass

        class _InlineCtx:
            def Queue(self):  # noqa: N802
                return queue_module.Queue()

            def Process(self, target, args, daemon):  # noqa: N802
                return _StubProcess()

        monkeypatch.setattr(orchestrator_mod.mp, "get_context", lambda _: _InlineCtx())

        orchestrator_mod._spawn_and_supervise(
            job_id=1,
            workspace=Path("/tmp"),
            source_ref_json='{"kind":"karakeep_bookmark","bookmark_id":"bm_x"}',
            poll_interval_seconds=0.001,
            timeout_seconds=0,
            is_cancelled_fn=lambda: False,
            config=ConversionConfig(_env_file=None),
            resource_guard=_TrackingGuard(),
            requires_gpu=False,
        )

        assert acquire_calls == [], "Guard must not be acquired when requires_gpu is False"


# ---------------------------------------------------------------------------
# run_worker concurrency
# ---------------------------------------------------------------------------


class TestRunWorkerConcurrency:
    def test_processes_up_to_concurrency_limit(self, monkeypatch):
        """Verify at most worker_concurrency jobs run simultaneously."""
        monkeypatch.setenv("WORKER_CONCURRENCY", "2")
        config = ConversionConfig(_env_file=None)

        max_concurrent = {"value": 0}
        current = {"value": 0}
        lock = threading.Lock()
        jobs_completed = {"count": 0}

        claim_count = {"n": 0}

        def _fake_claim(_config):
            claim_count["n"] += 1
            if claim_count["n"] <= 4:
                return claim_count["n"]
            shutdown.request_shutdown()
            return None

        def _fake_process(_job_id, _config, _runtime=None, poll_interval_seconds=2.0):
            with lock:
                current["value"] += 1
                max_concurrent["value"] = max(max_concurrent["value"], current["value"])
            time.sleep(0.03)
            with lock:
                current["value"] -= 1
                jobs_completed["count"] += 1

        monkeypatch.setattr(loop, "claim_next_job", _fake_claim)
        monkeypatch.setattr(loop, "process_job_supervised", _fake_process)
        monkeypatch.setattr(loop, "register_signal_handlers", lambda: None)
        monkeypatch.setattr(loop, "recover_stale_running_jobs", lambda _config: 0)
        # Patch build_worker_runtime so run_worker doesn't try to build a real runtime
        monkeypatch.setattr(
            "aizk.conversion.workers.loop.build_worker_runtime",
            lambda _cfg: _make_fake_runtime(),
        )

        exit_code = loop.run_worker(config, poll_interval_seconds=0.01)

        assert exit_code == 0
        assert max_concurrent["value"] <= 2
        assert jobs_completed["count"] == 4

    def test_shutdown_drains_all_in_flight_jobs(self, monkeypatch):
        """All in-flight jobs complete before exit on shutdown signal."""
        monkeypatch.setenv("WORKER_CONCURRENCY", "3")
        config = ConversionConfig(_env_file=None)

        completed_jobs = []
        lock = threading.Lock()
        claim_count = {"n": 0}

        def _fake_claim(_config):
            claim_count["n"] += 1
            if claim_count["n"] <= 3:
                return claim_count["n"]
            shutdown.request_shutdown()
            return None

        def _fake_process(job_id, _config, _runtime=None, poll_interval_seconds=2.0):
            time.sleep(0.03)
            with lock:
                completed_jobs.append(job_id)

        monkeypatch.setattr(loop, "claim_next_job", _fake_claim)
        monkeypatch.setattr(loop, "process_job_supervised", _fake_process)
        monkeypatch.setattr(loop, "register_signal_handlers", lambda: None)
        monkeypatch.setattr(loop, "recover_stale_running_jobs", lambda _config: 0)
        monkeypatch.setattr(
            "aizk.conversion.workers.loop.build_worker_runtime",
            lambda _cfg: _make_fake_runtime(),
        )

        exit_code = loop.run_worker(config, poll_interval_seconds=0.01)

        assert exit_code == 0
        assert sorted(completed_jobs) == [1, 2, 3]

    def test_shutdown_while_idle_exits_zero(self, monkeypatch):
        """Signal before any work exits cleanly."""
        shutdown.request_shutdown()

        monkeypatch.setattr(loop, "register_signal_handlers", lambda: None)
        monkeypatch.setattr(
            "aizk.conversion.workers.loop.build_worker_runtime",
            lambda _cfg: _make_fake_runtime(),
        )

        config = ConversionConfig(_env_file=None)
        exit_code = loop.run_worker(config, poll_interval_seconds=0.01)

        assert exit_code == 0
