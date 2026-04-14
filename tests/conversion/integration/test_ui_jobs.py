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
    assert "Delete" in body
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


def test_ui_jobs_delete_action_removes_failed_and_cancelled_jobs(db_session) -> None:
    app = create_app()
    bookmark = Bookmark(
        karakeep_id="bm_ui_delete",
        url="https://example.com/delete",
        normalized_url="https://example.com/delete",
        title="Delete Example",
        content_type="html",
        source_type="other",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    failed_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        attempts=1,
        idempotency_key="f" * 64,
    )
    cancelled_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.CANCELLED,
        attempts=2,
        idempotency_key="g" * 64,
    )
    db_session.add(failed_job)
    db_session.add(cancelled_job)
    db_session.commit()
    db_session.refresh(failed_job)
    db_session.refresh(cancelled_job)
    failed_job_id = failed_job.id
    cancelled_job_id = cancelled_job.id

    with TestClient(app) as client:
        response = client.post(
            "/ui/jobs/actions",
            data={"action": "delete", "job_ids": [failed_job_id, cancelled_job_id]},
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert "2 jobs deleted." in response.text
    assert "skipped as ineligible" not in response.text
    db_session.expire_all()
    assert db_session.get(ConversionJob, failed_job_id) is None
    assert db_session.get(ConversionJob, cancelled_job_id) is None


def test_ui_jobs_delete_action_rejects_non_deletable_status(db_session) -> None:
    app = create_app()
    bookmark = Bookmark(
        karakeep_id="bm_ui_delete_reject",
        url="https://example.com/delete-reject",
        normalized_url="https://example.com/delete-reject",
        title="Delete Reject Example",
        content_type="html",
        source_type="other",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    queued_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key="h" * 64,
    )
    db_session.add(queued_job)
    db_session.commit()
    db_session.refresh(queued_job)
    queued_job_id = queued_job.id

    with TestClient(app) as client:
        response = client.post(
            "/ui/jobs/actions",
            data={"action": "delete", "job_ids": [queued_job_id]},
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert "0 jobs deleted; 1 skipped as ineligible." in response.text
    db_session.expire_all()
    assert db_session.get(ConversionJob, queued_job_id) is not None


def test_ui_jobs_empty_unfiltered_shows_system_empty_message(db_session) -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/ui/jobs")

    assert response.status_code == 200
    assert "No jobs yet. Submit a bookmark to get started." in response.text
    assert "No jobs match your filters" not in response.text


def test_ui_jobs_search_with_no_matches_shows_filtered_empty_message(db_session) -> None:
    app = create_app()
    bookmark = Bookmark(
        karakeep_id="bm_ui_search_empty",
        url="https://example.com/search-empty",
        normalized_url="https://example.com/search-empty",
        title="Findable Title",
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
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
        idempotency_key="s" * 64,
    )
    db_session.add(job)
    db_session.commit()

    with TestClient(app) as client:
        response = client.get("/ui/jobs", params={"search": "no-such-term-zzz"})

    assert response.status_code == 200
    assert "No jobs match your filters" in response.text
    assert "No jobs yet. Submit a bookmark to get started." not in response.text


def test_ui_jobs_status_filter_with_no_matches_shows_filtered_empty_message(db_session) -> None:
    app = create_app()
    bookmark = Bookmark(
        karakeep_id="bm_ui_status_empty",
        url="https://example.com/status-empty",
        normalized_url="https://example.com/status-empty",
        title="Status Filter Example",
        content_type="html",
        source_type="other",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    succeeded_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
        idempotency_key="t" * 64,
    )
    queued_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key="u" * 64,
    )
    db_session.add(succeeded_job)
    db_session.add(queued_job)
    db_session.commit()

    with TestClient(app) as client:
        response = client.get("/ui/jobs", params={"status": "FAILED_PERM"})

    assert response.status_code == 200
    assert "No jobs match your filters" in response.text
    assert "No jobs yet. Submit a bookmark to get started." not in response.text


def test_ui_jobs_bulk_cancel_mixed_eligibility_splits_applied_and_ineligible(db_session) -> None:
    app = create_app()
    bookmark = Bookmark(
        karakeep_id="bm_ui_mixed_cancel",
        url="https://example.com/mixed-cancel",
        normalized_url="https://example.com/mixed-cancel",
        title="Mixed Cancel Example",
        content_type="html",
        source_type="other",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    queued_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key="q" * 64,
    )
    succeeded_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.SUCCEEDED,
        attempts=1,
        idempotency_key="v" * 64,
    )
    db_session.add(queued_job)
    db_session.add(succeeded_job)
    db_session.commit()
    db_session.refresh(queued_job)
    db_session.refresh(succeeded_job)
    queued_job_id = queued_job.id
    succeeded_job_id = succeeded_job.id

    with TestClient(app) as client:
        response = client.post(
            "/ui/jobs/actions",
            data={"action": "cancel", "job_ids": [queued_job_id, succeeded_job_id]},
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert "1 jobs cancelled" in response.text
    assert "1 skipped as ineligible" in response.text

    db_session.expire_all()
    assert db_session.get(ConversionJob, queued_job_id).status == ConversionJobStatus.CANCELLED
    assert db_session.get(ConversionJob, succeeded_job_id).status == ConversionJobStatus.SUCCEEDED


def test_ui_jobs_bulk_action_missing_job_counted_as_ineligible(db_session) -> None:
    app = create_app()
    bookmark = Bookmark(
        karakeep_id="bm_ui_missing_id",
        url="https://example.com/missing-id",
        normalized_url="https://example.com/missing-id",
        title="Missing ID Example",
        content_type="html",
        source_type="other",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    queued_job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key="w" * 64,
    )
    db_session.add(queued_job)
    db_session.commit()
    db_session.refresh(queued_job)
    queued_job_id = queued_job.id
    missing_job_id = 987654321

    with TestClient(app) as client:
        response = client.post(
            "/ui/jobs/actions",
            data={"action": "cancel", "job_ids": [queued_job_id, missing_job_id]},
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert "1 jobs cancelled" in response.text
    assert "1 skipped as ineligible" in response.text


def test_ui_jobs_bulk_action_all_eligible_omits_skipped_phrase(db_session) -> None:
    app = create_app()
    bookmark = Bookmark(
        karakeep_id="bm_ui_all_eligible",
        url="https://example.com/all-eligible",
        normalized_url="https://example.com/all-eligible",
        title="All Eligible Example",
        content_type="html",
        source_type="other",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    queued_a = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key="x" * 64,
    )
    queued_b = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key="y" * 64,
    )
    db_session.add(queued_a)
    db_session.add(queued_b)
    db_session.commit()
    db_session.refresh(queued_a)
    db_session.refresh(queued_b)

    with TestClient(app) as client:
        response = client.post(
            "/ui/jobs/actions",
            data={"action": "cancel", "job_ids": [queued_a.id, queued_b.id]},
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert "2 jobs cancelled." in response.text
    assert "skipped as ineligible" not in response.text
