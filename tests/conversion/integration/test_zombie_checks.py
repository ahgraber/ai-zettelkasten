"""Tests for the un-reaped-child detector the lifecycle suites assert with.

The lifecycle tests trust ``descendant_zombies`` to notice a subprocess the
runner failed to reap. A detector that reports a clean tree no matter what would
leave every one of those assertions passing for the wrong reason, so these tests
pin both directions: it finds a leak that exists, and it refuses to answer when
it cannot look.
"""

from __future__ import annotations

import subprocess
import sys
import time

import psutil
import pytest

from tests.conversion.integration._zombie_checks import ZombieInspectionError, descendant_zombies

pytestmark = [
    pytest.mark.isolate,  # Requires pytest-isolate: pip install pytest-isolate
    pytest.mark.integration_lifecycle,  # Custom marker for selective running
]

_DETECTION_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.05


def test_descendant_zombies_detects_an_un_reaped_child() -> None:
    """An exited-but-unwaited child is reported, so the lifecycle assertions have teeth."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    try:
        deadline = time.monotonic() + _DETECTION_TIMEOUT_SECONDS
        detected: list[str] = []
        while time.monotonic() < deadline:
            detected = [entry for entry in descendant_zombies() if f"pid={child.pid} " in entry]
            if detected:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        assert detected, f"the leaked child pid={child.pid} was not reported as un-reaped"
    finally:
        child.wait()


def test_descendant_zombies_reports_a_clean_tree_after_reaping() -> None:
    """A reaped child is not reported, so the detector does not fail a well-behaved runner."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    child.wait()

    assert [entry for entry in descendant_zombies() if f"pid={child.pid} " in entry] == []


def test_descendant_zombies_raises_when_the_process_table_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inspection that cannot run raises rather than reporting a clean tree.

    Returning an empty list here would make every lifecycle assertion pass on a
    host that never performed the check.
    """

    def raise_access_denied(*_args: object, **_kwargs: object) -> object:
        raise psutil.AccessDenied(pid=1)

    monkeypatch.setattr(psutil, "process_iter", raise_access_denied)

    with pytest.raises(ZombieInspectionError, match="could not enumerate processes"):
        descendant_zombies()
