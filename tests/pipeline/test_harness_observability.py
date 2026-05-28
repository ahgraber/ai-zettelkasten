"""Harness lifecycle-observability test.

Covers the spec requirement that the runtime emits structured logs carrying
trace context and operational metrics across the work-unit lifecycle, and that
the process advertises its stage role for operator monitoring.
"""

from __future__ import annotations

import logging

from pyleak import no_thread_leaks
import pytest

import aizk.pipeline.harness as harness_module
from aizk.pipeline.harness import InMemoryMetrics, StageHarness

from ._stub_repository import StubStageRepository, create_stub_engine


def test_lifecycle_logs_metrics_and_role(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Processing a unit emits lifecycle metrics, structured logs, and a stage role.

    Asserts the three observability strands together: operational metrics across
    the lifecycle (claimed → started → succeeded → cleaned_up), structured logs
    carrying trace context (stage + work_unit_ref), and a stage-role process
    title advertised for operators.
    """
    # Capture the stage-role process title without touching the real process.
    titles: list[str] = []
    monkeypatch.setattr(harness_module, "setproctitle", lambda title: titles.append(title))

    engine = create_stub_engine()
    repo = StubStageRepository(engine, stage_name="observable")
    unit_id = repo.enqueue("watched")

    metrics = InMemoryMetrics()
    harness = StageHarness(repo, engine, metrics=metrics, poll_interval=0.01)

    with caplog.at_level(logging.INFO, logger="aizk.pipeline.harness"), no_thread_leaks(action="raise"):
        harness.set_process_title()
        harness.run_until_idle()

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
