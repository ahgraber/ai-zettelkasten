"""Runner lifecycle-observability test.

Covers the spec requirement that the runtime emits structured logs carrying
trace context and operational metrics across the work-unit lifecycle, and that
the process advertises its stage role for operator monitoring.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pyleak import no_thread_leaks
import pytest
from sqlmodel import Session

from aizk.pipeline.lifecycle import WorkUnitStatus
import aizk.pipeline.runner as runner_module
from aizk.pipeline.runner import InMemoryMetrics, StageRunner

from ._stub_handler import StubStageHandler, StubWorkUnit, create_stub_engine, create_stub_file_engine


def test_lifecycle_logs_metrics_and_role(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Processing a unit emits lifecycle metrics, structured logs, and a stage role.

    Asserts the three observability strands together: operational metrics across
    the lifecycle (claimed → started → succeeded → cleaned_up), structured logs
    carrying trace context (stage + work_unit_ref), and a stage-role process
    title advertised for operators.
    """
    # Capture the stage-role process title without touching the real process.
    titles: list[str] = []
    monkeypatch.setattr(runner_module, "setproctitle", lambda title: titles.append(title))

    engine = create_stub_engine()
    handler = StubStageHandler(engine, stage_name="observable")
    unit_id = handler.enqueue("watched")

    metrics = InMemoryMetrics()
    runner = StageRunner(handler, engine, metrics=metrics, poll_interval=0.01)

    with caplog.at_level(logging.INFO, logger="aizk.pipeline.runner"), no_thread_leaks(action="raise"):
        runner.set_process_title()
        runner.run_until_idle()

    # Stage role advertised for operator monitoring.
    assert titles == ["aizk-stage-observable"], "process title advertises the stage role"

    # Operational metrics across the lifecycle.
    for counter in (
        "pipeline.startup.validated",
        "pipeline.work_unit.claimed",
        "pipeline.work_unit.started",
        "pipeline.work_unit.succeeded",
        "pipeline.work_unit.cleaned_up",
    ):
        assert metrics.counters.get(counter, 0) >= 1, f"missing lifecycle metric {counter}"

    # Lifecycle metrics carry the stage tag for cross-stage attribution.
    assert metrics.counter_tags.get("pipeline.work_unit.claimed", {}).get("stage") == "observable", (
        "lifecycle metric carries the stage tag"
    )

    # Structured logs carry trace context (stage + work-unit reference).
    claim_records = [r for r in caplog.records if r.message == "Claimed work-unit"]
    assert claim_records, "claim was logged"
    record = claim_records[0]
    assert getattr(record, "stage", None) == "observable", "log carries stage trace context"
    assert getattr(record, "work_unit_ref", None) == str(unit_id), "log carries work-unit reference"


def test_unwritten_finalize_counts_a_skip_not_the_outcome(tmp_path: Path) -> None:
    """A finalize that writes nothing counts ``finalize_skipped`` tagged with the outcome, not the outcome."""
    engine = create_stub_file_engine(str(tmp_path / "stub.db"), create_schema=True)
    handler = StubStageHandler(engine, stage_name="observable")

    def _cancelled_elsewhere(label: str) -> str:
        with Session(engine) as session:
            unit = session.get(StubWorkUnit, int(label))
            assert unit is not None
            unit.status = WorkUnitStatus.CANCELLED
            session.add(unit)
            session.commit()
        return "ok"

    unit_id = handler.enqueue("cancelled-elsewhere", behavior=_cancelled_elsewhere)
    metrics = InMemoryMetrics()
    runner = StageRunner(handler, engine, metrics=metrics, poll_interval=0.01)

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    assert handler.get_status(unit_id) == WorkUnitStatus.CANCELLED.value
    assert "pipeline.work_unit.succeeded" not in metrics.counters
    assert metrics.counters.get("pipeline.work_unit.finalize_skipped") == 1
    assert metrics.counter_tags["pipeline.work_unit.finalize_skipped"] == {
        "stage": "observable",
        "outcome": "succeeded",
    }
    assert handler.recorded.cleaned_up == [str(unit_id)]
