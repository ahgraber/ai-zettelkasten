"""Seeded timing tests for the console task monitor.

The monitor's list and count queries group over the existing indexed status
columns, so a large unit table renders a page quickly and a realistic bulk action
reports within the operator-facing time bounds. The thresholds are generous; the
tests guard against an accidental full-table scan or per-row query regression.
"""

from __future__ import annotations

import time

from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.graph.datamodel import ContextualizationJob
from aizk.pipeline.lifecycle import WorkUnitStatus


def _seed_jobs(session: Session, source_id, count: int, status: WorkUnitStatus) -> list[int]:
    """Bulk-insert ``count`` contextualization units for one source and return their ids."""
    jobs = [
        ContextualizationJob(
            idempotency_key=f"perf:{status.value}:{index}",
            conversion_output_id=index,
            source_id=source_id,
            status=status,
            attempts=0,
        )
        for index in range(count)
    ]
    session.add_all(jobs)
    session.commit()
    return [job.id for job in jobs]


def test_monitor_page_renders_within_two_seconds(
    client: TestClient, db_session: Session, seed_source
) -> None:
    """A 1000-unit stage monitor page renders within two seconds."""
    source = seed_source(db_session, karakeep_id="bm_perf_page", title="Perf Page Doc")
    _seed_jobs(db_session, source.source_id, 1000, WorkUnitStatus.QUEUED)

    start = time.perf_counter()
    response = client.get("/ui/tasks", params={"stage": "contextualization", "limit": 50})
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert "of 1000" in response.text
    assert elapsed < 2.0


def test_bulk_action_reports_within_five_seconds(
    client: TestClient, db_session: Session, seed_source
) -> None:
    """A bulk retry over a realistic (cap-sized) selection reports within five seconds."""
    source = seed_source(db_session, karakeep_id="bm_perf_bulk", title="Perf Bulk Doc")
    ids = _seed_jobs(db_session, source.source_id, 100, WorkUnitStatus.FAILED)

    start = time.perf_counter()
    response = client.post(
        "/ui/tasks/contextualization/actions", data={"action": "retry", "job_ids": ids}
    )
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert "100 jobs retried" in response.text
    assert elapsed < 5.0
