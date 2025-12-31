"""Integration tests for the jobs Web UI."""

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
    assert "Retry" in body
    assert "Cancel" in body
    assert 'id="status-filter"' in body
    assert 'id="text-filter"' in body
    assert "<style" in body
    assert "<script" in body
