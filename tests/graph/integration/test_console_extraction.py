"""Integration tests for the graph operator extraction jobs page.

Mirrors ``tests/graph/integration/test_ui_jobs.py`` (the contextualization jobs
page) at the same technique — driving the real graph operator app over a
migration-built SQLite database and asserting rendered HTML and committed
database state — scoped to the behaviors specific to the extraction stage's
single-run drill-down (as opposed to contextualization's three-stage
drill-down): the jobs table render and title resolution, status filtering,
bulk retry/cancel, and the per-job drill-down over the extraction run and the
work-unit event trail.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, RunStatus


def _seed_run(session: Session, *, source_id: UUID, status: RunStatus = RunStatus.ACTIVE) -> PipelineRun:
    """Insert one extraction run scoped to the source identity and return it."""
    run = PipelineRun(stage=EXTRACTION_STAGE, scope_id=str(source_id), status=status, derivation_key="dk")
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _seed_event(
    session: Session,
    *,
    job_id: int,
    source_id: UUID,
    kind: str,
    from_status: str | None,
    to_status: str | None,
    occurred_at: dt.datetime,
    attempt: int = 0,
) -> None:
    """Insert one work-unit lifecycle event on the extraction stage."""
    event = PipelineEvent(
        stage=EXTRACTION_STAGE,
        work_unit_ref=str(job_id),
        source_id=source_id,
        from_status=from_status,
        to_status=to_status,
        kind=kind,
        attempt=attempt,
        occurred_at=occurred_at,
        payload_json="{}",
    )
    session.add(event)
    session.commit()


# --- jobs table render + title resolution -----------------------------------


def test_jobs_table_renders_all_columns_on_load(
    client: TestClient, db_session, seed_source, seed_extraction_job
) -> None:
    """The jobs page renders a row with every contracted column on a plain load."""
    source = seed_source(db_session, karakeep_id="bm_render", title="Render Doc")
    job = seed_extraction_job(db_session, source_id=source.source_id, status=WorkUnitStatus.FAILED)

    response = client.get("/ui/tasks", params={"stage": "extraction"})

    assert response.status_code == 200
    body = response.text
    for header in (
        "Job ID",
        "source_id",
        "title",
        "status",
        "attempts",
        "queued_at",
        "started_at",
        "finished_at",
        "error_code",
    ):
        assert header in body
    assert str(job.id) in body
    assert "failed" in body


def test_jobs_table_title_uses_source_title_when_present(
    client: TestClient, db_session, seed_source, seed_extraction_job
) -> None:
    """The title cell shows the enriched ``Source.title`` when it is set."""
    source = seed_source(db_session, karakeep_id="bm_titled", title="Attention Is All You Need")
    seed_extraction_job(db_session, source_id=source.source_id)

    response = client.get("/ui/tasks", params={"stage": "extraction"})

    assert response.status_code == 200
    assert '<td class="title-cell">Attention Is All You Need</td>' in response.text


# --- filter across the full job set ------------------------------------------


def test_status_filter_excludes_other_statuses(
    client: TestClient, db_session, seed_source, seed_extraction_job
) -> None:
    """A status filter matches only jobs of that status, across the full set."""
    source = seed_source(db_session, karakeep_id="bm_filter", title="Filter Doc")
    failed = seed_extraction_job(db_session, source_id=source.source_id, status=WorkUnitStatus.FAILED)
    succeeded = seed_extraction_job(
        db_session, source_id=source.source_id, status=WorkUnitStatus.SUCCEEDED, idempotency_key="source:other"
    )

    response = client.get("/ui/tasks", params={"stage": "extraction", "status": "failed"})

    assert response.status_code == 200
    assert f'<td class="mono">{failed.id}</td>' in response.text
    assert f'<td class="mono">{succeeded.id}</td>' not in response.text


# --- bulk actions -------------------------------------------------------------


def test_bulk_retry_requeues_eligible_jobs_with_summary(
    client: TestClient, db_session, seed_source, seed_extraction_job
) -> None:
    """Bulk retry returns the selected eligible jobs to queued and reports a summary."""
    source = seed_source(db_session, karakeep_id="bm_retry", title="Retry Doc")
    job_a = seed_extraction_job(
        db_session, source_id=source.source_id, status=WorkUnitStatus.FAILED, idempotency_key="source:a"
    )
    job_b = seed_extraction_job(
        db_session, source_id=source.source_id, status=WorkUnitStatus.FAILED, idempotency_key="source:b"
    )

    response = client.post(
        "/ui/tasks/extraction/actions", data={"action": "retry", "job_ids": [job_a.id, job_b.id]}
    )

    assert response.status_code == 200
    assert "2 jobs retried" in response.text
    db_session.expire_all()
    assert db_session.get(ExtractionJob, job_a.id).status is WorkUnitStatus.QUEUED
    assert db_session.get(ExtractionJob, job_b.id).status is WorkUnitStatus.QUEUED


def test_bulk_cancel_cancels_eligible_jobs_with_summary(
    client: TestClient, db_session, seed_source, seed_extraction_job
) -> None:
    """Bulk cancel attempts cancellation on the selected jobs and reports a summary."""
    source = seed_source(db_session, karakeep_id="bm_cancel", title="Cancel Doc")
    job_a = seed_extraction_job(
        db_session, source_id=source.source_id, status=WorkUnitStatus.QUEUED, idempotency_key="source:a"
    )
    job_b = seed_extraction_job(
        db_session, source_id=source.source_id, status=WorkUnitStatus.RUNNING, idempotency_key="source:b"
    )

    response = client.post(
        "/ui/tasks/extraction/actions", data={"action": "cancel", "job_ids": [job_a.id, job_b.id]}
    )

    assert response.status_code == 200
    assert "2 jobs cancelled" in response.text
    db_session.expire_all()
    assert db_session.get(ExtractionJob, job_a.id).status is WorkUnitStatus.CANCELLED
    assert db_session.get(ExtractionJob, job_b.id).status is WorkUnitStatus.CANCELLED


# --- drill-down ---------------------------------------------------------------


def test_completed_job_drilldown_shows_the_run_and_succeeded_trail(
    client: TestClient, db_session, seed_source, seed_extraction_job
) -> None:
    """A completed job's drill-down shows the extraction run and an event trail ending in succeeded."""
    source = seed_source(db_session, karakeep_id="bm_done", title="Done Doc")
    job = seed_extraction_job(db_session, source_id=source.source_id, status=WorkUnitStatus.SUCCEEDED)
    _seed_run(db_session, source_id=source.source_id, status=RunStatus.ACTIVE)
    base = dt.datetime(2026, 7, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    _seed_event(
        db_session,
        job_id=job.id,
        source_id=source.source_id,
        kind="claimed",
        from_status="queued",
        to_status="running",
        occurred_at=base,
    )
    _seed_event(
        db_session,
        job_id=job.id,
        source_id=source.source_id,
        kind="succeeded",
        from_status="running",
        to_status="succeeded",
        occurred_at=base + dt.timedelta(seconds=1),
    )

    response = client.get(f"/ui/tasks/extraction/{job.id}")

    assert response.status_code == 200
    body = response.text
    assert "Mention Extraction" in body
    assert 'data-present="true"' in body
    assert 'data-present="false"' not in body
    assert "active" in body
    assert body.index("claimed") < body.index("succeeded")


def test_never_extracted_job_drilldown_shows_absent_run(
    client: TestClient, db_session, seed_source, seed_extraction_job
) -> None:
    """A job with no extraction run yet shows the run as absent."""
    source = seed_source(db_session, karakeep_id="bm_gap", title="Gap Doc")
    job = seed_extraction_job(db_session, source_id=source.source_id, status=WorkUnitStatus.FAILED)
    base = dt.datetime(2026, 7, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
    _seed_event(
        db_session,
        job_id=job.id,
        source_id=source.source_id,
        kind="failed",
        from_status="running",
        to_status="failed",
        occurred_at=base,
    )

    response = client.get(f"/ui/tasks/extraction/{job.id}")

    assert response.status_code == 200
    body = response.text
    assert 'data-present="false"' in body
    assert "absent" in body
    assert '<li class="event failed">' in body


def test_job_stages_unknown_job_is_404(client: TestClient) -> None:
    """Requesting the drill-down for an unknown work-unit returns 404."""
    response = client.get("/ui/tasks/extraction/999999")

    assert response.status_code == 404
