"""Tests for the API-side stale-job reaper loop (F12, #33)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from aizk.conversion.api import main as api_main


class _CountingReaper:
    """Records each call and controls how many times the loop body runs."""

    def __init__(self, reap_counts: list[int]) -> None:
        self._reap_counts = list(reap_counts)
        self.calls = 0

    def __call__(self, _config) -> int:
        self.calls += 1
        if self._reap_counts:
            return self._reap_counts.pop(0)
        return 0


@pytest.fixture()
def fast_config(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Config stub with a near-zero reap interval so tests run fast."""
    cfg = MagicMock()
    cfg.worker_stale_job_check_seconds = 0.01
    cfg.worker_stale_job_minutes = 30
    return cfg


async def _run_loop_for(monkeypatch, config, reaper_impl, iterations: int):
    """Drive the reaper loop and cancel after ``iterations`` completed sleeps."""
    module = MagicMock()
    module.recover_stale_running_jobs = reaper_impl

    with patch.dict(
        "sys.modules", {"aizk.conversion.workers.loop": module}
    ):
        task = asyncio.create_task(api_main._stale_job_reaper_loop(config))
        # Yield the loop long enough for `iterations` full sleep/reap cycles.
        for _ in range(iterations + 1):
            await asyncio.sleep(config.worker_stale_job_check_seconds * 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_reaper_calls_recover_periodically(fast_config) -> None:
    reaper = _CountingReaper([0, 0])
    await _run_loop_for(None, fast_config, reaper, iterations=2)
    assert reaper.calls >= 2


@pytest.mark.asyncio
async def test_reaper_logs_warning_when_rows_reaped(fast_config, caplog) -> None:
    reaper = _CountingReaper([3, 0])
    caplog.set_level("WARNING", logger="aizk.conversion.api.main")
    await _run_loop_for(None, fast_config, reaper, iterations=2)
    assert any(
        "stale-job reaper recovered 3 RUNNING job(s)" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_reaper_survives_exceptions_in_reap_iteration(fast_config, caplog) -> None:
    """A transient DB error in one reap iteration must not kill the loop."""
    call_log: list[str] = []

    def _flaky_reaper(_config) -> int:
        call_log.append("ping")
        if len(call_log) == 1:
            raise RuntimeError("boom")
        return 0

    caplog.set_level("ERROR", logger="aizk.conversion.api.main")
    await _run_loop_for(None, fast_config, _flaky_reaper, iterations=3)

    # At least 2 invocations: the one that raised + at least one after.
    assert len(call_log) >= 2
    assert any("stale-job reaper iteration failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_reaper_is_cancellable() -> None:
    """Cancelling the task re-raises CancelledError rather than silently noop-ing."""
    cfg = MagicMock()
    cfg.worker_stale_job_check_seconds = 60.0  # won't fire during test
    cfg.worker_stale_job_minutes = 30

    reaper = _CountingReaper([])
    module = MagicMock()
    module.recover_stale_running_jobs = reaper

    with patch.dict("sys.modules", {"aizk.conversion.workers.loop": module}):
        task = asyncio.create_task(api_main._stale_job_reaper_loop(cfg))
        await asyncio.sleep(0.01)  # let it enter the loop
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
