"""Integration tests for the console dashboard and the descriptor registration seam.

The dashboard (``GET /ui``) folds each registered stage's native-status counts onto
the generic lifecycle vocabulary and, where a stage declares the ``failed_split``
capability, subdivides its ``FAILED`` count into units awaiting an automatic retry
and units that have exhausted retries. These tests pin that read-side rollup for the
graph stages and, using test-double descriptors registered into a live app, the two
contracts that make the console descriptor-driven: a newly registered stage becomes
fully operable (dashboard, monitor, filter, drill-down) with no route or template
change, and a stage's declared capabilities (which actions, whether a detail
section) govern what its surfaces offer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
import datetime as dt
import re

import pytest
from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.console import descriptors as descriptors_module
from aizk.console.descriptors import StageDescriptor
from aizk.console.stages._graph import build_graph_descriptor
from aizk.graph.api.routes import _apply_cancel, _apply_retry
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.lifecycle import WorkUnitStatus


def _make_double(key: str, label: str, **overrides: object) -> StageDescriptor:
    """Build a graph-shaped descriptor backed by ``ContextualizationJob`` for tests.

    The base is a full graph stage descriptor under a fresh ``key``; ``overrides`` are
    applied with :func:`dataclasses.replace` so a test can, for example, strip the
    declared actions or the drill-down detail composer to exercise a stage's declared
    capabilities.
    """
    base = build_graph_descriptor(
        key=key,
        label=label,
        model=ContextualizationJob,
        events_stage=CONTEXTUALIZATION_STAGE,
        drilldown_stages=[(CHUNKING_STAGE, "Chunking")],
        apply_retry=_apply_retry,
        apply_cancel=_apply_cancel,
        id_search_columns=[],
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture
def register_double() -> Iterator[Callable[[StageDescriptor], StageDescriptor]]:
    """Register test-double descriptors into the live registry and remove them after.

    Only the keys this test registered are removed on teardown, so the shipped
    contextualization/extraction registrations are left intact for other tests.
    """
    added: list[str] = []

    def _register(descriptor: StageDescriptor) -> StageDescriptor:
        descriptors_module.register_stage(descriptor)
        added.append(descriptor.key)
        return descriptor

    yield _register

    for key in added:
        descriptors_module._REGISTRY.pop(key, None)


def _dashboard_row(body: str, stage_key: str) -> str:
    """Return the dashboard table row for ``stage_key`` (fails if absent)."""
    match = re.search(rf'<tr data-stage="{stage_key}".*?</tr>', body, re.DOTALL)
    assert match is not None, f"no dashboard row rendered for stage {stage_key!r}"
    return match.group(0)


# --- graph-stage rollup with failed split ------------------------------------


def test_dashboard_shows_graph_stage_counts_split_by_retry_disposition(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """The graph stage's counts appear per lifecycle category, failed split by retry."""
    source = seed_source(db_session, karakeep_id="bm_dash", title="Dashboard Doc")
    for index in range(2):
        seed_contextualization_job(
            db_session, source_id=source.source_id, conversion_output_id=index, status=WorkUnitStatus.QUEUED
        )
    seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=2, status=WorkUnitStatus.SUCCEEDED
    )
    awaiting = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=3, status=WorkUnitStatus.FAILED
    )
    awaiting.earliest_next_attempt_at = dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.timezone.utc)
    db_session.add(awaiting)
    for index in (4, 5):
        seed_contextualization_job(
            db_session, source_id=source.source_id, conversion_output_id=index, status=WorkUnitStatus.FAILED
        )
    db_session.commit()

    response = client.get("/ui")

    assert response.status_code == 200
    row = _dashboard_row(response.text, "contextualization")
    assert re.search(r'class="count queued"[^>]*>\s*2', row)
    assert re.search(r'class="count succeeded"[^>]*>\s*1', row)
    # The failed cell carries the split: one awaiting an automatic retry
    # (earliest_next_attempt_at set), two permanent (NULL).
    assert 'data-awaiting-retry="1"' in row
    assert 'data-permanent="2"' in row
    assert "1 awaiting retry, 2 permanent" in row
    assert '<td class="count total">6</td>' in row


def test_dashboard_shows_zeroes_for_a_stage_with_no_units(client: TestClient, migrated_engine) -> None:
    """A registered stage with no work-units renders a zero row, not an omission."""
    response = client.get("/ui")

    assert response.status_code == 200
    row = _dashboard_row(response.text, "extraction")
    assert '<td class="count total">0</td>' in row


# --- registration seam --------------------------------------------------------


def test_registered_double_is_fully_operable_without_route_or_template_change(
    client: TestClient,
    db_session: Session,
    seed_source,
    seed_contextualization_job,
    register_double,
) -> None:
    """A descriptor registered at runtime lists, filters, and drills down generically.

    The double is backed by ``ContextualizationJob`` under a fresh key; registering it
    alone — touching no route or template — must give it a dashboard row, a monitor
    listing, a working status filter, and a drill-down.
    """
    register_double(_make_double("double", "Test Double"))
    source = seed_source(db_session, karakeep_id="bm_seam", title="Seam Doc")
    failed = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=10, status=WorkUnitStatus.FAILED
    )
    queued = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=11, status=WorkUnitStatus.QUEUED
    )

    # Dashboard: the double appears as its own stage row.
    dashboard = client.get("/ui")
    assert dashboard.status_code == 200
    assert 'data-stage="double"' in dashboard.text
    assert "Test Double" in dashboard.text

    # Monitor: the double lists its work-units.
    monitor = client.get("/ui/tasks", params={"stage": "double"})
    assert monitor.status_code == 200
    assert f'<td class="mono">{failed.id}</td>' in monitor.text
    assert f'<td class="mono">{queued.id}</td>' in monitor.text

    # Filter: a status filter narrows the list through the generic path.
    filtered = client.get("/ui/tasks", params={"stage": "double", "status": "failed"})
    assert filtered.status_code == 200
    assert f'<td class="mono">{failed.id}</td>' in filtered.text
    assert f'<td class="mono">{queued.id}</td>' not in filtered.text

    # Drill-down: the double resolves a unit and renders its event trail.
    drilldown = client.get(f"/ui/tasks/double/{failed.id}")
    assert drilldown.status_code == 200
    assert "Work-unit event trail" in drilldown.text


# --- declared capabilities ----------------------------------------------------


def test_stage_declaring_no_actions_offers_no_action_controls(
    client: TestClient,
    db_session: Session,
    seed_source,
    seed_contextualization_job,
    register_double,
) -> None:
    """A stage declaring no actions renders no action buttons in its monitor."""
    register_double(_make_double("noactions", "No Actions", actions=[]))
    source = seed_source(db_session, karakeep_id="bm_noact", title="No-Actions Doc")
    seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=20, status=WorkUnitStatus.FAILED
    )

    response = client.get("/ui/tasks", params={"stage": "noactions"})

    assert response.status_code == 200
    # The bulk-action buttons are the only markup carrying ``name="action"``.
    assert 'name="action"' not in response.text


@pytest.mark.parametrize("action", ["retry", "cancel"])
def test_stage_declaring_no_actions_rejects_every_action(
    client: TestClient,
    db_session: Session,
    seed_source,
    seed_contextualization_job,
    register_double,
    action: str,
) -> None:
    """A stage declaring no actions rejects any submitted action as undeclared (400)."""
    register_double(_make_double("noactions", "No Actions", actions=[]))
    source = seed_source(db_session, karakeep_id="bm_noact2", title="No-Actions Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=21, status=WorkUnitStatus.FAILED
    )

    response = client.post("/ui/tasks/noactions/actions", data={"action": action, "job_ids": [job.id]})

    assert response.status_code == 400
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, job.id).status is WorkUnitStatus.FAILED


def test_stage_declaring_no_detail_renders_event_trail_alone(
    client: TestClient,
    db_session: Session,
    seed_source,
    seed_contextualization_job,
    register_double,
) -> None:
    """A stage declaring no detail section shows only the event trail in its drill-down."""
    register_double(_make_double("nodetail", "No Detail", detail=None, detail_template=None))
    source = seed_source(db_session, karakeep_id="bm_nodetail", title="No-Detail Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=30, status=WorkUnitStatus.FAILED
    )

    response = client.get(f"/ui/tasks/nodetail/{job.id}")

    assert response.status_code == 200
    assert "Work-unit event trail" in response.text
    # The stage-runs detail section is a declared capability the double omits.
    assert "Stage runs" not in response.text
    assert 'class="stage-run' not in response.text
