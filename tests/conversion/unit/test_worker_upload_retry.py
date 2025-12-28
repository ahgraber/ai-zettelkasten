"""Unit tests for worker upload retry behavior."""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from aizk.conversion.workers import worker
from aizk.conversion.workers.worker import ConversionInput
from aizk.datamodel.bookmark import Bookmark
from aizk.datamodel.job import ConversionJob, ConversionJobStatus


def test_process_job_retries_upload(monkeypatch, db_session: Session, html_bookmark) -> None:
    """Verify upload retries without invoking real conversion or network calls."""
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "1")

    # Seed a bookmark/job so process_job can move through its normal workflow.
    bookmark = Bookmark(
        karakeep_id="bm_retry_test",
        url="https://example.com",
        normalized_url="https://example.com",
        title="Retry Test",
        content_type="html",
        source_type="web",
    )
    db_session.add(bookmark)
    db_session.commit()
    db_session.refresh(bookmark)

    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        idempotency_key="a" * 64,
        status=ConversionJobStatus.QUEUED,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    def _prepare_conversion_input(**_kwargs):
        # Force a deterministic conversion input to avoid calling external services.
        return ConversionInput(
            pipeline="html",
            content_bytes=b"<html><body>test</body></html>",
            fetched_at=dt.datetime.now(dt.timezone.utc),
        )

    # Bypass network and conversion steps so we can focus on the upload retry loop.
    monkeypatch.setattr(worker, "fetch_karakeep_bookmark", lambda _karakeep_id: html_bookmark)
    monkeypatch.setattr(worker, "validate_bookmark_content", lambda _bookmark: None)
    monkeypatch.setattr(worker, "_prepare_conversion_input", _prepare_conversion_input)
    monkeypatch.setattr(worker, "_run_conversion", lambda **_kwargs: None)

    upload_attempts = {"count": 0}
    sleep_calls: list[float] = []
    handle_errors = {"count": 0}

    def _upload_converted(_job_id, _workspace):
        # Fail twice to exercise retry backoff, then succeed.
        upload_attempts["count"] += 1
        if upload_attempts["count"] < 3:
            raise RuntimeError("transient upload failure")

    def _handle_job_error(_job_id, _error):
        # Track error handling to ensure we don't mark the job as failed on success.
        handle_errors["count"] += 1

    monkeypatch.setattr(worker, "_upload_converted", _upload_converted)
    monkeypatch.setattr(worker, "handle_job_error", _handle_job_error)
    # Capture sleep durations instead of actually sleeping.
    monkeypatch.setattr(worker.time, "sleep", lambda delay: sleep_calls.append(delay))

    worker.process_job(job.id)

    assert upload_attempts["count"] == 3
    assert sleep_calls == [1, 2]
    assert handle_errors["count"] == 0
