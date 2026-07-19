"""Boundary-validation tests for the console's unified task routes.

The console validates operator input before touching any work-unit: an unknown
stage key or unit id is not-found, an undeclared action is rejected, an empty
selection alters nothing with an informative summary, and a selection above the
cap is rejected whole. These are the console's external-boundary guarantees on its
one mutating surface.
"""

from __future__ import annotations

from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.console.routes import MAX_BULK_SELECTION
from aizk.graph.datamodel import ContextualizationJob
from aizk.pipeline.lifecycle import WorkUnitStatus


def test_unknown_stage_is_not_found_on_every_route(client: TestClient) -> None:
    """An unregistered stage key is not-found on the monitor, drill-down, and action routes."""
    monitor = client.get("/ui/tasks", params={"stage": "nope"})
    drilldown = client.get("/ui/tasks/nope/1")
    action = client.post("/ui/tasks/nope/actions", data={"action": "retry", "job_ids": [1]})

    assert monitor.status_code == 404
    assert drilldown.status_code == 404
    assert action.status_code == 404


def test_unknown_unit_drilldown_is_not_found(client: TestClient) -> None:
    """A drill-down for a unit id that does not exist for the stage is not-found."""
    response = client.get("/ui/tasks/contextualization/999999")

    assert response.status_code == 404


def test_undeclared_action_is_rejected_without_mutation(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """An action a stage does not declare is rejected (400) and alters no unit."""
    source = seed_source(db_session, karakeep_id="bm_undeclared", title="Undeclared Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=81, status=WorkUnitStatus.FAILED
    )

    # Graph stages declare only Retry and Cancel; Delete is not offered here.
    response = client.post("/ui/tasks/contextualization/actions", data={"action": "delete", "job_ids": [job.id]})

    assert response.status_code == 400
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, job.id).status is WorkUnitStatus.FAILED


def test_empty_selection_alters_nothing_with_informative_summary(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """A bulk action with no units selected alters nothing and returns an informative notice."""
    source = seed_source(db_session, karakeep_id="bm_empty", title="Empty Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=82, status=WorkUnitStatus.FAILED
    )

    response = client.post("/ui/tasks/contextualization/actions", data={"action": "retry"})

    assert response.status_code == 200
    assert "Select at least one work-unit." in response.text
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, job.id).status is WorkUnitStatus.FAILED


def test_oversized_selection_is_rejected_atomically(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """A selection above the cap is rejected whole, altering no unit in the selection."""
    source = seed_source(db_session, karakeep_id="bm_oversized", title="Oversized Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=83, status=WorkUnitStatus.FAILED
    )
    # One real, retryable id padded out past the cap with throwaway ids.
    selection = [job.id, *range(10_000, 10_000 + MAX_BULK_SELECTION)]
    assert len(selection) > MAX_BULK_SELECTION

    response = client.post("/ui/tasks/contextualization/actions", data={"action": "retry", "job_ids": selection})

    assert response.status_code == 400
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, job.id).status is WorkUnitStatus.FAILED


def test_malformed_status_filter_is_rejected_before_any_mutation(
    client: TestClient, db_session: Session, seed_source, seed_contextualization_job
) -> None:
    """A malformed carried status filter rejects the action (400) without applying it.

    The status filter is validated at the boundary, before the write transaction, so
    a bad value cannot commit the action and then fail on the panel re-render.
    """
    source = seed_source(db_session, karakeep_id="bm_badstatus", title="Bad Status Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=84, status=WorkUnitStatus.FAILED
    )

    response = client.post(
        "/ui/tasks/contextualization/actions",
        data={"action": "retry", "job_ids": [job.id], "status": "bogus-status"},
    )

    assert response.status_code == 400
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, job.id).status is WorkUnitStatus.FAILED
