"""Concurrency tests for conversion worker job pickup."""

from __future__ import annotations

import datetime as dt
import threading
from uuid import UUID

from sqlalchemy.orm import Mapped

from aizk.conversion.datamodel.bookmark import Bookmark
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers import worker as worker_module


def test_poll_and_process_jobs_is_atomic(db_session, monkeypatch):
    """Ensure only one worker claims a queued job when polling concurrently."""
    bookmark = Bookmark(
        karakeep_id="bm_concurrent_001",
        aizk_uuid=UUID("550e8400-e29b-41d4-a716-446655440000"),
        url="https://example.com",
        normalized_url="https://example.com",
        title="Concurrency Test",
        content_type="html",
        source_type="other",
        created_at=dt.datetime.now(dt.timezone.utc),
        updated_at=dt.datetime.now(dt.timezone.utc),
    )
    db_session.add(bookmark)
    db_session.commit()

    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        title=bookmark.title,
        payload_version=1,
        status=ConversionJobStatus.QUEUED,
        attempts=0,
        idempotency_key="deadbeef" * 8,
        queued_at=dt.datetime.now(dt.timezone.utc),
        created_at=dt.datetime.now(dt.timezone.utc),
        updated_at=dt.datetime.now(dt.timezone.utc),
    )
    db_session.add(job)
    db_session.commit()

    # Avoid running the full job pipeline; this test only cares about the claim step.
    monkeypatch.setattr(
        worker_module, "process_job_supervised", lambda _job_id, _config, poll_interval_seconds=2.0: None
    )

    config = ConversionConfig(_env_file=None)

    # Two workers start at the same time to contend for the same queued job.
    barrier = threading.Barrier(3)
    results: list[bool] = []
    lock = threading.Lock()

    def _runner() -> None:
        barrier.wait()
        result = worker_module.poll_and_process_jobs(config)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=_runner) for _ in range(2)]
    for thread in threads:
        thread.start()

    barrier.wait()
    for thread in threads:
        thread.join()

    # Exactly one worker should claim the job and mark it RUNNING.
    assert results.count(True) == 1
    assert results.count(False) == 1

    db_session.refresh(job)
    assert job.status == ConversionJobStatus.RUNNING
    assert job.attempts == 1
