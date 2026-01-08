"""Integration tests for the jobs Web UI."""

import datetime as dt

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus


def test_ui_jobs_renders_table_and_filters(db_session) -> None:
    app = create_app()
    bookmark = Bookmark(
        karakeep_id="bm_ui_jobs",
        url="https://example.com/ui",
        normalized_url="https://example.com/ui",
        title="UI Example",
        content_type="html",
        source_type="other",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.FAILED_PERM,
        attempts=2,
        idempotency_key="e" * 64,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    with TestClient(app) as client:
        response = client.get("/ui/jobs")

    assert response.status_code == 200
    body = response.text
    assert "<table" in body
    assert 'hx-get="/ui/jobs"' in body
    assert "htmx.org" in body
    assert 'id="jobs-panel"' in body
    assert "Job ID" in body
    assert "aizk_uuid" in body
    assert "karakeep_id" in body
    assert "title" in body
    assert "status" in body
    assert "attempts" in body
    assert "queued_at" in body
    assert "started_at" in body
    assert "finished_at" in body
    assert "error_code" in body
    assert str(job.id) in body
    assert 'id="status-filter"' in body
    assert 'id="text-filter"' in body
    assert "Matches:" in body
    assert "Selected:" in body


def test_ui_jobs_filters_across_all_jobs(db_session) -> None:
    app = create_app()
    base_time = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)

    target_bookmark = Bookmark(
        karakeep_id="bm_ui_target",
        url="https://example.com/target",
        normalized_url="https://example.com/target",
        title="Special Target",
        content_type="html",
        source_type="other",
    )
    db_session.add(target_bookmark)
    db_session.commit()
    db_session.refresh(target_bookmark)

    target_job = ConversionJob(
        aizk_uuid=target_bookmark.aizk_uuid,
        title="Special Target",
        payload_version=1,
        status=ConversionJobStatus.FAILED_PERM,
        attempts=1,
        idempotency_key="target-idempotency-key",
        queued_at=base_time,
        created_at=base_time,
        updated_at=base_time,
    )
    db_session.add(target_job)

    for idx in range(60):
        bookmark = Bookmark(
            karakeep_id=f"bm_ui_jobs_{idx}",
            url=f"https://example.com/{idx}",
            normalized_url=f"https://example.com/{idx}",
            title=f"Noise {idx}",
            content_type="html",
            source_type="other",
        )
        db_session.add(bookmark)
        queued_at = base_time + dt.timedelta(minutes=idx + 1)
        job = ConversionJob(
            aizk_uuid=bookmark.aizk_uuid,
            title=bookmark.title,
            payload_version=1,
            status=ConversionJobStatus.SUCCEEDED,
            attempts=1,
            idempotency_key=f"idempotency-{idx}",
            queued_at=queued_at,
            created_at=queued_at,
            updated_at=queued_at,
        )
        db_session.add(job)

    db_session.commit()
    db_session.refresh(target_job)

    with TestClient(app) as client:
        first_page = client.get("/ui/jobs", params={"limit": 50})
        assert "Special Target" not in first_page.text

        filtered = client.get("/ui/jobs", params={"search": "Special Target", "limit": 50})

    assert filtered.status_code == 200
    assert str(target_job.id) in filtered.text
    assert "Special Target" in filtered.text
    assert 'id="filtered-count">1' in filtered.text
