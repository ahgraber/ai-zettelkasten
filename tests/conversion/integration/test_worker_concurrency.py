"""Integration tests for the conversion GPU resource guard.

The engine-loop concerns this file used to cover — ``claim_next_in_session``
atomicity under contention, ThreadPoolExecutor concurrency limits, and shutdown drain
semantics — are now owned by the pipeline runner and covered by
``tests/pipeline/test_runner_scheduling.py`` (bounded concurrency + claim order),
``tests/pipeline/test_runner_drain.py`` (drain), and
``tests/conversion/unit/test_handler.py`` (claim/recover transactionality).

What remains here is conversion-specific: the GPU resource guard that
:func:`~aizk.conversion.workers.orchestrator._spawn_and_supervise` acquires only
when the converter requires the GPU, and releases on every subprocess outcome.
"""

from __future__ import annotations

import threading
import time

from aizk.conversion.utilities.config import ConversionConfig


class TestGpuSemaphoreGuard:
    def test_semaphore_guard_limits_concurrent_access(self):
        """Verify only N threads can hold the semaphore simultaneously."""
        from aizk.conversion.wiring.worker import _SemaphoreGuard

        sem = threading.Semaphore(1)
        guard = _SemaphoreGuard(sem)

        max_concurrent = 0
        current = 0
        lock = threading.Lock()

        def _worker():
            nonlocal max_concurrent, current
            with guard:
                with lock:
                    current += 1
                    max_concurrent = max(max_concurrent, current)
                time.sleep(0.01)
                with lock:
                    current -= 1

        threads = [threading.Thread(target=_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert max_concurrent == 1

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
            workspace=Path("/tmp"),  # noqa: S108
            source_ref_json='{"kind":"karakeep_bookmark","bookmark_id":"bm_x"}',
            poll_interval_seconds=0.001,
            timeout_seconds=0,
            is_cancelled_fn=lambda: False,
            config=ConversionConfig(_env_file=None),
            resource_guard=_TrackingGuard(),
            requires_gpu=False,
        )

        assert acquire_calls == [], "Guard must not be acquired when requires_gpu is False"

    def test_guard_released_after_subprocess_crash(self, monkeypatch):
        """Spec: guard SHALL be released when the subprocess crashes (non-zero exitcode)."""
        from pathlib import Path
        import queue as queue_module

        from aizk.conversion.wiring.worker import _SemaphoreGuard
        from aizk.conversion.workers import orchestrator as orchestrator_mod

        class _CrashedProcess:
            pid = None
            exitcode = 1

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
                return _CrashedProcess()

        monkeypatch.setattr(orchestrator_mod.mp, "get_context", lambda _: _InlineCtx())

        sem = threading.BoundedSemaphore(1)
        guard = _SemaphoreGuard(sem)

        orchestrator_mod._spawn_and_supervise(
            job_id=1,
            workspace=Path("/tmp"),  # noqa: S108
            source_ref_json='{"kind":"karakeep_bookmark","bookmark_id":"bm_x"}',
            poll_interval_seconds=0.001,
            timeout_seconds=0,
            is_cancelled_fn=lambda: False,
            config=ConversionConfig(_env_file=None),
            resource_guard=guard,
            requires_gpu=True,
        )

        assert sem.acquire(blocking=False), "Guard must be released after subprocess crash"
        sem.release()

    def test_guard_released_after_timeout(self, monkeypatch):
        """Spec: guard SHALL be released when the supervisor terminates the subprocess on timeout."""
        from pathlib import Path
        import queue as queue_module

        from aizk.conversion.wiring.worker import _SemaphoreGuard
        from aizk.conversion.workers import orchestrator as orchestrator_mod, supervision as supervision_mod

        class _LingeringProcess:
            """Alive on first poll so the deadline check fires; dead afterward."""

            pid = None
            exitcode = -15

            def __init__(self):
                self._alive = True

            def start(self):
                pass

            def is_alive(self):
                # Flip to dead after the supervisor calls terminate_and_wait.
                alive = self._alive
                self._alive = False
                return alive

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
                return _LingeringProcess()

        monkeypatch.setattr(orchestrator_mod.mp, "get_context", lambda _: _InlineCtx())

        # Deterministic clock: deadline = 0 + 1 = 1; supervisor's first check sees t=2 ≥ 1.
        clock = iter([0.0, 2.0, 2.0, 2.0, 2.0])
        monkeypatch.setattr(orchestrator_mod.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(supervision_mod.time, "monotonic", lambda: next(clock))

        sem = threading.BoundedSemaphore(1)
        guard = _SemaphoreGuard(sem)

        _, result, _ = orchestrator_mod._spawn_and_supervise(
            job_id=2,
            workspace=Path("/tmp"),  # noqa: S108
            source_ref_json='{"kind":"karakeep_bookmark","bookmark_id":"bm_x"}',
            poll_interval_seconds=0.001,
            timeout_seconds=1.0,
            is_cancelled_fn=lambda: False,
            config=ConversionConfig(_env_file=None),
            resource_guard=guard,
            requires_gpu=True,
        )

        assert result.timed_out is True
        assert sem.acquire(blocking=False), "Guard must be released after timeout termination"
        sem.release()
