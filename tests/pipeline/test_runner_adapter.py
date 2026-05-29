"""Runner-over-handler-protocol tests.

Covers the spec requirement that the runner drives work-unit processing purely
through a stage-supplied :class:`~aizk.pipeline.handler.StageHandler`: a
new stage runs by supplying an adapter, and two stages with different stores
share one runner, each through its own handler.
"""

from __future__ import annotations

from pyleak import no_thread_leaks

from aizk.pipeline.runner import StageRunner
from aizk.pipeline.lifecycle import WorkUnitStatus

from ._stub_handler import StubStageHandler, create_stub_engine


def test_new_stage_runs_via_adapter() -> None:
    """A stage runs purely by supplying a handler — no runner change.

    The runner has no knowledge of the stub's schema; it drives discovery,
    claim, execution, finalization, and cleanup entirely through the supplied
    handler, and the unit reaches a terminal outcome.
    """
    engine = create_stub_engine()
    handler = StubStageHandler(engine, concurrency_limit=2)
    unit_id = handler.enqueue("only")

    runner = StageRunner(handler, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    assert handler.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value
    assert handler.recorded.cleaned_up == [str(unit_id)], "cleanup ran for the unit"


def test_two_stores_share_runner() -> None:
    """Two stages with different stores each run through their own handler.

    Distinct engines, distinct stage identities, distinct runner instances —
    the same runner type drives both without any shared work-unit table.
    """
    engine_a = create_stub_engine()
    engine_b = create_stub_engine()
    handler_a = StubStageHandler(engine_a, stage_name="alpha")
    handler_b = StubStageHandler(engine_b, stage_name="beta")
    a_id = handler_a.enqueue("a")
    b_id = handler_b.enqueue("b")

    runner_a = StageRunner(handler_a, engine_a, poll_interval=0.01)
    runner_b = StageRunner(handler_b, engine_b, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner_a.run_until_idle()
        runner_b.run_until_idle()

    assert handler_a.get_status(a_id) == WorkUnitStatus.SUCCEEDED.value
    assert handler_b.get_status(b_id) == WorkUnitStatus.SUCCEEDED.value
