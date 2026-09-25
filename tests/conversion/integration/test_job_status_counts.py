"""Integration tests for job status counts endpoint."""

from __future__ import annotations

from sqlmodel import Session

from fastapi.testclient import TestClient

from aizk.conversion.api.dependencies import get_db_session
from aizk.conversion.api.main import create_app
from aizk.conversion.datamodel.job import ConversionJob
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.utilities.clock import utcnow
from tests.conversion._helpers import make_job, make_source


def test_status_counts_returns_totals(db_session) -> None:
    app = create_app()
    bookmark = make_source(db_session, "bm_status_counts")
    make_job(db_session, source_id=bookmark.source_id, status=WorkUnitStatus.QUEUED, idempotency_key="a" * 64)
    make_job(db_session, source_id=bookmark.source_id, status=WorkUnitStatus.QUEUED, idempotency_key="b" * 64)
    make_job(
        db_session,
        source_id=bookmark.source_id,
        status=WorkUnitStatus.FAILED,
        earliest_next_attempt_at=utcnow(),
        idempotency_key="c" * 64,
    )

    with TestClient(app) as client:
        response = client.get("/v1/jobs/status-counts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["counts"] == {"queued": 2, "failed": 1}


def test_status_counts_empty_returns_zero(db_session) -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/v1/jobs/status-counts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0
    assert payload["counts"] == {}


def test_status_counts_key_each_of_the_six_statuses(db_session) -> None:
    """One job in each lifecycle status yields exactly the six status keys."""
    bookmark = make_source(db_session, "bm_six_statuses")
    for index, status in enumerate(WorkUnitStatus):
        make_job(db_session, source_id=bookmark.source_id, status=status, idempotency_key=f"six-{index}")

    with TestClient(create_app()) as client:
        payload = client.get("/v1/jobs/status-counts").json()

    assert payload["counts"] == dict.fromkeys(
        ("queued", "running", "succeeded", "failed", "cancelled", "timed_out"), 1
    )
    assert payload["total"] == 6


def test_status_counts_split_failed_by_retry_class(db_session) -> None:
    """Two failed jobs with a retry wait and one without split as ``{"retryable": 2, "permanent": 1}``."""
    bookmark = make_source(db_session, "bm_failed_split")
    for key in ("a", "b"):
        make_job(
            db_session,
            source_id=bookmark.source_id,
            status=WorkUnitStatus.FAILED,
            earliest_next_attempt_at=utcnow(),
            idempotency_key=key * 64,
        )
    make_job(db_session, source_id=bookmark.source_id, status=WorkUnitStatus.FAILED, idempotency_key="c" * 64)

    with TestClient(create_app()) as client:
        payload = client.get("/v1/jobs/status-counts").json()

    assert payload["counts"]["failed"] == 3
    assert payload["failed_by_retry_class"] == {"retryable": 2, "permanent": 1}


def test_status_counts_split_is_present_with_no_failed_job(db_session) -> None:
    """The retry-class split is ``{"retryable": 0, "permanent": 0}`` when no job has failed."""
    bookmark = make_source(db_session, "bm_no_failed")
    make_job(db_session, source_id=bookmark.source_id, status=WorkUnitStatus.QUEUED, idempotency_key="a" * 64)

    with TestClient(create_app()) as client:
        payload = client.get("/v1/jobs/status-counts").json()

    assert payload["failed_by_retry_class"] == {"retryable": 0, "permanent": 0}


def test_status_counts_split_sums_to_the_failed_count(db_session) -> None:
    """Across a mix of statuses with and without a retry wait, the split sums to ``counts.failed`` and each status counts once."""
    bookmark = make_source(db_session, "bm_split_sum")
    seeded = [
        (WorkUnitStatus.QUEUED, False),
        (WorkUnitStatus.QUEUED, True),
        (WorkUnitStatus.RUNNING, False),
        (WorkUnitStatus.SUCCEEDED, False),
        (WorkUnitStatus.FAILED, True),
        (WorkUnitStatus.FAILED, True),
        (WorkUnitStatus.FAILED, False),
        (WorkUnitStatus.CANCELLED, False),
        (WorkUnitStatus.TIMED_OUT, False),
    ]
    for index, (status, has_wait) in enumerate(seeded):
        make_job(
            db_session,
            source_id=bookmark.source_id,
            status=status,
            earliest_next_attempt_at=utcnow() if has_wait else None,
            idempotency_key=f"sum-{index}",
        )

    with TestClient(create_app()) as client:
        payload = client.get("/v1/jobs/status-counts").json()

    assert payload["counts"] == {
        "queued": 2,
        "running": 1,
        "succeeded": 1,
        "failed": 3,
        "cancelled": 1,
        "timed_out": 1,
    }
    assert payload["total"] == 9
    split = payload["failed_by_retry_class"]
    assert split["retryable"] + split["permanent"] == payload["counts"].get("failed", 0)


def test_status_counts_are_one_read_a_concurrent_claim_cannot_split(db_session) -> None:
    """A retryable job claimed before any second read of the request leaves the split equal to ``counts.failed``."""
    bookmark = make_source(db_session, "bm_split_race")
    retryable = [
        make_job(
            db_session,
            source_id=bookmark.source_id,
            status=WorkUnitStatus.FAILED,
            earliest_next_attempt_at=utcnow(),
            idempotency_key=key * 64,
        )
        for key in ("a", "b")
    ]
    make_job(db_session, source_id=bookmark.source_id, status=WorkUnitStatus.FAILED, idempotency_key="c" * 64)
    engine = db_session.get_bind()
    claimed_id = retryable[0].id

    class _ClaimBeforeSecondRead(Session):
        reads = 0

        def exec(self, *args, **kwargs):
            self.reads += 1
            if self.reads == 2:
                with Session(engine) as other:
                    job = other.get(ConversionJob, claimed_id)
                    job.status = WorkUnitStatus.RUNNING
                    job.earliest_next_attempt_at = None
                    other.add(job)
                    other.commit()
            return super().exec(*args, **kwargs)

    def _session():
        with _ClaimBeforeSecondRead(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db_session] = _session
    with TestClient(app) as client:
        payload = client.get("/v1/jobs/status-counts").json()

    split = payload["failed_by_retry_class"]
    assert split["retryable"] + split["permanent"] == payload["counts"].get("failed", 0)
    assert split == {"retryable": 2, "permanent": 1}


def test_status_counts_split_counts_only_the_callers_jobs(db_session) -> None:
    """Another principal's failed jobs are absent from the retry-class split."""
    bookmark = make_source(db_session, "bm_split_owner")
    make_job(
        db_session,
        source_id=bookmark.source_id,
        status=WorkUnitStatus.FAILED,
        earliest_next_attempt_at=utcnow(),
        idempotency_key="a" * 64,
        owner_id="someone_else",
    )
    make_job(
        db_session,
        source_id=bookmark.source_id,
        status=WorkUnitStatus.FAILED,
        idempotency_key="b" * 64,
        owner_id="someone_else",
    )

    with TestClient(create_app()) as client:
        payload = client.get("/v1/jobs/status-counts").json()

    assert payload["failed_by_retry_class"] == {"retryable": 0, "permanent": 0}
