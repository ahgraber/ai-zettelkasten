"""Tests for the stale-window startup check in :func:`aizk.conversion.processing.worker.run_worker`."""

from __future__ import annotations

import pytest

from aizk.conversion.processing.worker import run_worker
from aizk.conversion.settings import ConversionConfig
from aizk.conversion.wiring import worker as wiring_worker
from aizk.storage.settings import ObjectStorageSettings
from aizk.utilities.errors import StartupValidationError


class _RuntimeBuiltError(Exception):
    """Raised by the stubbed runtime builder to show the check let startup through."""


@pytest.fixture()
def built(monkeypatch: pytest.MonkeyPatch) -> list[ConversionConfig]:
    """Replace the runtime builder with one that records its call and stops startup."""
    calls: list[ConversionConfig] = []

    def _build(config: ConversionConfig, **_kwargs: object) -> None:
        calls.append(config)
        raise _RuntimeBuiltError

    monkeypatch.setattr(wiring_worker, "build_worker_runtime", _build)
    return calls


def test_run_worker_refuses_a_stale_window_inside_the_timeout_and_drain(built: list[ConversionConfig]) -> None:
    """A 30-minute stale window under the default 2-hour timeout is refused before the runtime is built."""
    config = ConversionConfig(_env_file=None, worker_stale_job_minutes=30)

    with pytest.raises(StartupValidationError):
        run_worker(config, object_storage=ObjectStorageSettings(_env_file=None))

    assert built == []


def test_run_worker_accepts_the_derived_stale_window(built: list[ConversionConfig]) -> None:
    """The derived default passes the check, so startup reaches the runtime builder."""
    config = ConversionConfig(_env_file=None)

    with pytest.raises(_RuntimeBuiltError):
        run_worker(config, object_storage=ObjectStorageSettings(_env_file=None))

    assert built == [config]
