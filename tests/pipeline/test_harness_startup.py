"""Harness startup dependency-validation test.

Covers the spec requirement that the runtime validates its required external
dependencies at startup and does not begin accepting work-units until validation
succeeds.
"""

from __future__ import annotations

import threading

from pyleak import no_thread_leaks
import pytest

from aizk.pipeline.harness import StageHarness
from aizk.pipeline.lifecycle import WorkUnitStatus

from ._stub_repository import StubStageRepository, create_stub_engine


def test_missing_dependency_blocks_acceptance() -> None:
    """A missing dependency fails startup validation and blocks work acceptance.

    With the repository's dependency check configured to fail, the harness
    raises during validation and never claims or executes the queued unit, which
    remains queued.
    """
    engine = create_stub_engine()
    repo = StubStageRepository(engine, dependencies_ok=False)
    unit_id = repo.enqueue("blocked")

    harness = StageHarness(repo, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"), pytest.raises(RuntimeError, match="dependency unavailable"):
        harness.run_until_idle()

    assert repo.recorded.execute_started == [], "no work-unit was executed before validation passed"
    assert repo.get_status(unit_id) == WorkUnitStatus.QUEUED.value, "the unit stays queued"


def test_validation_success_allows_acceptance() -> None:
    """When validation passes, the harness accepts and processes work."""
    engine = create_stub_engine()
    repo = StubStageRepository(engine, dependencies_ok=True)
    unit_id = repo.enqueue("runnable")

    harness = StageHarness(repo, engine, poll_interval=0.01)

    def _drive() -> None:
        harness.run_until_idle()

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=_drive)
        driver.start()
        driver.join(timeout=10.0)
        assert not driver.is_alive()

    assert repo.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value
