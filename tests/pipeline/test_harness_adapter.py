"""Harness-over-repository-protocol tests.

Covers the spec requirement that the harness drives work-unit processing purely
through a stage-supplied :class:`~aizk.pipeline.repository.StageRepository`: a
new stage runs by supplying an adapter, and two stages with different stores
share one harness, each through its own repository.
"""

from __future__ import annotations

from pyleak import no_thread_leaks

from aizk.pipeline.harness import StageHarness
from aizk.pipeline.lifecycle import WorkUnitStatus

from ._stub_repository import StubStageRepository, create_stub_engine


def test_new_stage_runs_via_adapter() -> None:
    """A stage runs purely by supplying a repository — no harness change.

    The harness has no knowledge of the stub's schema; it drives discovery,
    claim, execution, finalization, and cleanup entirely through the supplied
    repository, and the unit reaches a terminal outcome.
    """
    engine = create_stub_engine()
    repo = StubStageRepository(engine, concurrency_limit=2)
    unit_id = repo.enqueue("only")

    harness = StageHarness(repo, engine, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        harness.run_until_idle()

    assert repo.get_status(unit_id) == WorkUnitStatus.SUCCEEDED.value
    assert repo.recorded.cleaned_up == [str(unit_id)], "cleanup ran for the unit"


def test_two_stores_share_harness() -> None:
    """Two stages with different stores each run through their own repository.

    Distinct engines, distinct stage identities, distinct harness instances —
    the same harness type drives both without any shared work-unit table.
    """
    engine_a = create_stub_engine()
    engine_b = create_stub_engine()
    repo_a = StubStageRepository(engine_a, stage_name="alpha")
    repo_b = StubStageRepository(engine_b, stage_name="beta")
    a_id = repo_a.enqueue("a")
    b_id = repo_b.enqueue("b")

    harness_a = StageHarness(repo_a, engine_a, poll_interval=0.01)
    harness_b = StageHarness(repo_b, engine_b, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        harness_a.run_until_idle()
        harness_b.run_until_idle()

    assert repo_a.get_status(a_id) == WorkUnitStatus.SUCCEEDED.value
    assert repo_b.get_status(b_id) == WorkUnitStatus.SUCCEEDED.value
