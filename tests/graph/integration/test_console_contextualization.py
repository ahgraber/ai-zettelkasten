"""Integration tests for the graph operator contextualization jobs page.

Drives the real graph operator app over a migration-built SQLite database (see the
package ``conftest``) and asserts the rendered HTML and the committed database
state. Covers the jobs table render and title resolution, status/text filtering
across the full job set, bulk retry/cancel summaries and their write effects, and
the per-job stage drill-down composed from stage runs and the work-unit event
trail.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.graph.contextualization import SUMMARY_STAGE, VARIANT_STAGE
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, RunStatus


def _seed_run(
    session: Session,
    *,
    stage: str,
    source_id: UUID,
    status: RunStatus = RunStatus.ACTIVE,
) -> PipelineRun:
    """Insert one stage run scoped to the source identity and return it."""
    run = PipelineRun(stage=stage, scope_id=str(source_id), status=status, derivation_key="dk")
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
    """Insert one work-unit lifecycle event on the contextualization stage."""
    event = PipelineEvent(
        stage=CONTEXTUALIZATION_STAGE,
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
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """The jobs page renders a row with every contracted column on a plain load."""
    source = seed_source(db_session, karakeep_id="bm_render", title="Render Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=11, status=WorkUnitStatus.FAILED
    )

    response = client.get("/ui/tasks", params={"stage": "contextualization"})

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
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """The title cell shows the enriched ``Source.title`` when it is set."""
    source = seed_source(db_session, karakeep_id="bm_titled", title="Attention Is All You Need")
    seed_contextualization_job(db_session, source_id=source.source_id, conversion_output_id=12)

    response = client.get("/ui/tasks", params={"stage": "contextualization"})

    assert response.status_code == 200
    assert '<td class="title-cell">Attention Is All You Need</td>' in response.text


def test_jobs_table_title_falls_back_to_source_id_when_title_null(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """The title cell falls back to the source ``source_id`` when ``Source.title`` is NULL."""
    source = seed_source(db_session, karakeep_id="bm_untitled", title=None)
    seed_contextualization_job(db_session, source_id=source.source_id, conversion_output_id=13)

    response = client.get("/ui/tasks", params={"stage": "contextualization"})

    assert response.status_code == 200
    assert f'<td class="title-cell">{source.source_id}</td>' in response.text


# --- filter + search across the full job set --------------------------------


def test_status_filter_spans_full_set_and_excludes_other_statuses(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """A status filter matches jobs beyond the current page and excludes other statuses."""
    source = seed_source(db_session, karakeep_id="bm_filter", title="Filter Doc")
    failed_ids = {
        seed_contextualization_job(
            db_session, source_id=source.source_id, conversion_output_id=cid, status=WorkUnitStatus.FAILED
        ).id
        for cid in (1, 2, 3, 4, 5)
    }
    succeeded_ids = {
        seed_contextualization_job(
            db_session, source_id=source.source_id, conversion_output_id=cid, status=WorkUnitStatus.SUCCEEDED
        ).id
        for cid in (100, 101, 102)
    }

    page1 = client.get("/ui/tasks", params={"stage": "contextualization", "status": "failed", "limit": 2, "offset": 0})
    page2 = client.get("/ui/tasks", params={"stage": "contextualization", "status": "failed", "limit": 2, "offset": 2})
    page3 = client.get("/ui/tasks", params={"stage": "contextualization", "status": "failed", "limit": 2, "offset": 4})

    assert page1.status_code == 200
    # The filtered total counts the whole matching set, not just the current page.
    assert "of 5" in page1.text
    seen = set()
    for page in (page1, page2, page3):
        for fid in failed_ids:
            if f'<td class="mono">{fid}</td>' in page.text:
                seen.add(fid)
    # All five matching jobs are reachable across pages (i.e. from beyond page one).
    assert seen == failed_ids
    # No job of another status appears under the filter.
    for sid in succeeded_ids:
        for page in (page1, page2, page3):
            assert f'<td class="mono">{sid}</td>' not in page.text


def test_search_by_source_title_finds_job(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """A text search on the source title returns the matching job."""
    source = seed_source(db_session, karakeep_id="bm_search", title="Attention Is All You Need")
    job = seed_contextualization_job(db_session, source_id=source.source_id, conversion_output_id=21)

    response = client.get("/ui/tasks", params={"stage": "contextualization", "search": "attention"})

    assert response.status_code == 200
    assert f'<td class="mono">{job.id}</td>' in response.text
    assert "No jobs match your filters" not in response.text


def test_search_with_no_match_renders_empty_state(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """A search term matching nothing renders the empty state, not a stale list."""
    source = seed_source(db_session, karakeep_id="bm_nomatch", title="Attention Is All You Need")
    seed_contextualization_job(db_session, source_id=source.source_id, conversion_output_id=22)

    response = client.get("/ui/tasks", params={"stage": "contextualization", "search": "zzz-no-such-term"})

    assert response.status_code == 200
    assert "No jobs match your filters" in response.text


# --- bulk actions -----------------------------------------------------------


def test_bulk_retry_requeues_eligible_jobs_with_summary(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """Bulk retry returns the selected eligible jobs to queued and reports a summary."""
    source = seed_source(db_session, karakeep_id="bm_retry", title="Retry Doc")
    job_a = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=31, status=WorkUnitStatus.FAILED
    )
    job_b = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=32, status=WorkUnitStatus.FAILED
    )

    response = client.post("/ui/tasks/contextualization/actions", data={"action": "retry", "job_ids": [job_a.id, job_b.id]})

    assert response.status_code == 200
    assert "2 jobs retried" in response.text
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, job_a.id).status is WorkUnitStatus.QUEUED
    assert db_session.get(ContextualizationJob, job_b.id).status is WorkUnitStatus.QUEUED


def test_bulk_cancel_cancels_eligible_jobs_with_summary(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """Bulk cancel attempts cancellation on the selected jobs and reports a summary."""
    source = seed_source(db_session, karakeep_id="bm_cancel", title="Cancel Doc")
    job_a = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=41, status=WorkUnitStatus.QUEUED
    )
    job_b = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=42, status=WorkUnitStatus.RUNNING
    )

    response = client.post("/ui/tasks/contextualization/actions", data={"action": "cancel", "job_ids": [job_a.id, job_b.id]})

    assert response.status_code == 200
    assert "2 jobs cancelled" in response.text
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, job_a.id).status is WorkUnitStatus.CANCELLED
    assert db_session.get(ContextualizationJob, job_b.id).status is WorkUnitStatus.CANCELLED


def test_bulk_action_mixed_eligibility_distinguishes_applied_and_skipped(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """A mixed-eligibility bulk retry applies the eligible job and skips the ineligible one untouched."""
    source = seed_source(db_session, karakeep_id="bm_mixed", title="Mixed Doc")
    eligible = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=51, status=WorkUnitStatus.FAILED
    )
    ineligible = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=52, status=WorkUnitStatus.SUCCEEDED
    )

    response = client.post("/ui/tasks/contextualization/actions", data={"action": "retry", "job_ids": [eligible.id, ineligible.id]})

    assert response.status_code == 200
    assert "1 jobs retried" in response.text
    assert "1 skipped as ineligible" in response.text
    db_session.expire_all()
    assert db_session.get(ContextualizationJob, eligible.id).status is WorkUnitStatus.QUEUED
    # The ineligible job's status is unchanged.
    assert db_session.get(ContextualizationJob, ineligible.id).status is WorkUnitStatus.SUCCEEDED


# --- stage drill-down -------------------------------------------------------


def test_completed_job_drilldown_shows_all_runs_and_succeeded_trail(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """A completed job's drill-down shows all three stage runs and an event trail ending in succeeded."""
    source = seed_source(db_session, karakeep_id="bm_done", title="Done Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=61, status=WorkUnitStatus.SUCCEEDED
    )
    for stage in (CHUNKING_STAGE, SUMMARY_STAGE, VARIANT_STAGE):
        _seed_run(db_session, stage=stage, source_id=source.source_id, status=RunStatus.ACTIVE)
    base = dt.datetime(2026, 6, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
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

    response = client.get(f"/ui/tasks/contextualization/{job.id}")

    assert response.status_code == 200
    body = response.text
    for label in ("Chunking", "Document Summary", "Chunk Contextualization"):
        assert label in body
    # All three stage runs render present (not just one); none is absent.
    assert body.count('data-present="true"') == 3
    assert 'data-present="false"' not in body
    assert "active" in body
    # The trail is chronological and ends in the succeeded event.
    assert "claimed" in body
    assert body.index("claimed") < body.index("succeeded")


def test_chunked_not_contextualized_drilldown_shows_gap_and_failure(
    client: TestClient, db_session, seed_source, seed_contextualization_job
) -> None:
    """A job chunked but not contextualized shows chunking present, the variant absent, and the failure event."""
    source = seed_source(db_session, karakeep_id="bm_gap", title="Gap Doc")
    job = seed_contextualization_job(
        db_session, source_id=source.source_id, conversion_output_id=71, status=WorkUnitStatus.FAILED
    )
    _seed_run(db_session, stage=CHUNKING_STAGE, source_id=source.source_id, status=RunStatus.ACTIVE)
    base = dt.datetime(2026, 6, 13, 12, 0, 0, tzinfo=dt.timezone.utc)
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
        kind="failed",
        from_status="running",
        to_status="failed",
        occurred_at=base + dt.timedelta(seconds=1),
    )

    response = client.get(f"/ui/tasks/contextualization/{job.id}")

    assert response.status_code == 200
    body = response.text
    assert '<li class="stage-run chunking" data-present="true">' in body
    assert '<li class="stage-run chunk_contextualization" data-present="false">' in body
    assert "absent" in body
    # The failure is surfaced from the work-unit event trail, not merely the word "failed".
    assert '<li class="event failed">' in body


def test_job_stages_unknown_job_is_404(client: TestClient) -> None:
    """Requesting the drill-down for an unknown work-unit returns 404."""
    response = client.get("/ui/tasks/contextualization/999999")

    assert response.status_code == 404
