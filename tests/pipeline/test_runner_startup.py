"""Runner startup dependency-validation test.

Covers the spec requirement that the runtime validates its required external
dependencies at startup and does not begin accepting work-units until validation
succeeds.
"""

from __future__ import annotations

import threading

from pyleak import no_thread_leaks
import pytest

from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.runner import StageRunner

from ._stub_handler import StubStageHandler, create_stub_engine


def test_missing_dependency_blocks_acceptance() -> None:
    """A missing dependency fails startup validation and blocks work acceptance.

    With the handler's dependency check configured to fail, the runner
    raises during validation and never claims or executes the queued unit, which
    remains queued.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, dependencies_ok=False)
    unit_id = handler.enqueue("blocked")

    runner = StageRunner(handler, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"), pytest.raises(RuntimeError, match="dependency unavailable"):
        runner.run_until_idle()

    assert handler.recorded.execute_started == [], "no work-unit was executed before validation passed"
    assert handler.get_status(unit_id) == WorkUnitStatus.QUEUED.value, "the unit stays queued"


def test_validation_success_allows_acceptance() -> None:
    """When validation passes, the runner accepts and processes work."""
    engine = create_stub_engine()
    handler = StubStageHandler(engine, dependencies_ok=True)
    unit_id = handler.enqueue("runnable")

    runner = StageRunner(handler, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        driver = threading.Thread(target=runner.run_until_idle)
        driver.start()
        driver.join(timeout=10.0)
        assert not driver.is_alive()

    assert handler.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value
