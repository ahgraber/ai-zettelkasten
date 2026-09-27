"""Integration tests for job bulk actions."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.settings import ConversionConfig
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.utilities.clock import utcnow
from tests.conversion._helpers import make_job, make_source


def test_bulk_retry_resets_failed_jobs(db_session) -> None:
    app = create_app()
    bookmark = make_source(db_session, "bm_bulk_retry")
    job_retryable = make_job(
        db_session,
        source_id=bookmark.source_id,
        status=WorkUnitStatus.FAILED,
        idempotency_key="a" * 64,
        attempts=1,
        earliest_next_attempt_at=utcnow(),
    )
    job_cancelled = make_job(
        db_session,
        source_id=bookmark.source_id,
        status=WorkUnitStatus.CANCELLED,
        idempotency_key="b" * 64,
        attempts=2,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs/actions",
            json={"action": "retry", "job_ids": [job_retryable.id, job_cancelled.id]},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "retry"
    assert payload["summary"]["success"] == 2
    assert payload["summary"]["errors"] == 0

    result_statuses = {item["job_id"]: item["status"] for item in payload["results"]}
    assert result_statuses[job_retryable.id] == "success"
    assert result_statuses[job_cancelled.id] == "success"

    db_session.refresh(job_retryable)
    db_session.refresh(job_cancelled)
    assert job_retryable.status == WorkUnitStatus.QUEUED
    assert job_retryable.attempts == 1
    assert job_cancelled.status == WorkUnitStatus.QUEUED
    assert job_cancelled.attempts == 2


def test_single_retry_keeps_attempt_count(db_session) -> None:
    app = create_app()
    bookmark = make_source(db_session, "bm_single_retry")
    job = make_job(
        db_session,
        source_id=bookmark.source_id,
        status=WorkUnitStatus.FAILED,
        idempotency_key="e" * 64,
        attempts=3,
        earliest_next_attempt_at=utcnow(),
    )

    with TestClient(app) as client:
        response = client.post(f"/v1/jobs/{job.id}/retry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["attempts"] == 3

    db_session.refresh(job)
    assert job.attempts == 3


def test_bulk_cancel_marks_queued_and_running_jobs(db_session) -> None:
    app = create_app()
    bookmark = make_source(db_session, "bm_bulk_cancel")
    job_queued = make_job(
        db_session,
        source_id=bookmark.source_id,
        status=WorkUnitStatus.QUEUED,
        idempotency_key="c" * 64,
    )
    job_running = make_job(
        db_session,
        source_id=bookmark.source_id,
        status=WorkUnitStatus.RUNNING,
        idempotency_key="d" * 64,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs/actions",
            json={"action": "cancel", "job_ids": [job_queued.id, job_running.id]},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "cancel"
    assert payload["summary"]["success"] == 2
    assert payload["summary"]["errors"] == 0

    result_statuses = {item["job_id"]: item["status"] for item in payload["results"]}
    assert result_statuses[job_queued.id] == "success"
    assert result_statuses[job_running.id] == "success"

    db_session.refresh(job_queued)
    db_session.refresh(job_running)
    assert job_queued.status == WorkUnitStatus.CANCELLED
    assert job_running.status == WorkUnitStatus.CANCELLED


_NOT_RETRYABLE = {"detail": {"error": "job_not_retryable", "message": "Job cannot be retried"}}
_NOT_CANCELLABLE = {"detail": {"error": "job_not_cancellable", "message": "Job cannot be cancelled"}}


def _event_count(session: Session) -> int:
    """Count every recorded lifecycle event."""
    return len(session.exec(select(PipelineEvent)).all())


@pytest.mark.parametrize("status", [WorkUnitStatus.FAILED, WorkUnitStatus.CANCELLED, WorkUnitStatus.TIMED_OUT])
def test_single_retry_requeues_a_terminal_job_keeping_its_attempts(db_session, status: WorkUnitStatus) -> None:
    """A retry from ``failed``, ``cancelled``, or ``timed_out`` returns the job ``queued`` with ``attempts`` unchanged."""
    source = make_source(db_session, "bm_retry_terminal")
    job = make_job(db_session, source_id=source.source_id, idempotency_key="t" * 64, status=status, attempts=2)

    with TestClient(create_app()) as client:
        response = client.post(f"/v1/jobs/{job.id}/retry")

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["attempts"]) == ("queued", 2)


@pytest.mark.parametrize(
    ("status", "phase"),
    [(WorkUnitStatus.QUEUED, None), (WorkUnitStatus.RUNNING, "convert"), (WorkUnitStatus.SUCCEEDED, None)],
)
def test_single_retry_of_an_ineligible_job_answers_409(db_session, status: WorkUnitStatus, phase: str | None) -> None:
    """A retry from ``queued``, ``running``, or ``succeeded`` answers 409 with the ``job_not_retryable`` body and alters nothing."""
    source = make_source(db_session, "bm_retry_ineligible")
    job = make_job(
        db_session, source_id=source.source_id, idempotency_key="i" * 64, status=status, attempts=1, phase=phase
    )
    job_before, events_before = job.model_dump(), _event_count(db_session)

    with TestClient(create_app()) as client:
        response = client.post(f"/v1/jobs/{job.id}/retry")

    assert response.status_code == 409
    assert response.json() == _NOT_RETRYABLE
    db_session.refresh(job)
    assert job.model_dump() == job_before
    assert _event_count(db_session) == events_before


@pytest.mark.parametrize(
    ("status", "phase", "wait"),
    [
        (WorkUnitStatus.QUEUED, None, None),
        (WorkUnitStatus.RUNNING, "convert", None),
        (WorkUnitStatus.FAILED, None, dt.timedelta(minutes=-1)),
    ],
)
def test_single_cancel_ends_a_queued_running_or_failed_job_unclaimable(
    db_session, status: WorkUnitStatus, phase: str | None, wait: dt.timedelta | None
) -> None:
    """A cancel of a ``queued`` job, a converting job, or a failed job whose wait has elapsed ends it ``cancelled``, unclaimable."""
    source = make_source(db_session, "bm_cancel_eligible")
    job = make_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="e" * 64,
        status=status,
        phase=phase,
        earliest_next_attempt_at=utcnow() + wait if wait is not None else None,
    )

    with TestClient(create_app()) as client:
        response = client.post(f"/v1/jobs/{job.id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["earliest_next_attempt_at"], body["phase"]) == ("cancelled", None, None)
    with Session(db_session.get_bind()) as session:
        assert ConversionStageHandler(ConversionConfig(_env_file=None)).claim_next(session) is None


@pytest.mark.parametrize(
    ("status", "phase"),
    [
        (WorkUnitStatus.RUNNING, "upload"),
        (WorkUnitStatus.SUCCEEDED, None),
        (WorkUnitStatus.CANCELLED, None),
        (WorkUnitStatus.TIMED_OUT, None),
    ],
)
def test_single_cancel_of_an_ineligible_job_answers_409(db_session, status: WorkUnitStatus, phase: str | None) -> None:
    """A cancel of an uploading, ``succeeded``, ``cancelled``, or ``timed_out`` job answers 409 with the ``job_not_cancellable`` body and alters nothing."""
    source = make_source(db_session, "bm_cancel_ineligible")
    job = make_job(db_session, source_id=source.source_id, idempotency_key="n" * 64, status=status, phase=phase)
    job_before, events_before = job.model_dump(), _event_count(db_session)

    with TestClient(create_app()) as client:
        response = client.post(f"/v1/jobs/{job.id}/cancel")

    assert response.status_code == 409
    assert response.json() == _NOT_CANCELLABLE
    db_session.refresh(job)
    assert job.model_dump() == job_before
    assert _event_count(db_session) == events_before


def test_cancelling_a_failed_job_keeps_its_error_code_and_message(db_session) -> None:
    """A cancelled failed job still carries the error code and message it failed with."""
    source = make_source(db_session, "bm_cancel_keeps_error")
    job = make_job(db_session, source_id=source.source_id, idempotency_key="k" * 64, status=WorkUnitStatus.FAILED)
    job.error_code = "fetch_failed"
    job.error_message = "upstream returned 500"
    db_session.add(job)
    db_session.commit()

    with TestClient(create_app()) as client:
        response = client.post(f"/v1/jobs/{job.id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["error_code"], body["error_message"]) == (
        "cancelled",
        "fetch_failed",
        "upstream returned 500",
    )


def test_bulk_cancel_skips_an_uploading_job_as_not_cancellable(db_session) -> None:
    """A bulk cancel of a queued job and an uploading job answers 200, cancels the first, and reports the second ``job_not_cancellable``."""
    source = make_source(db_session, "bm_bulk_upload")
    queued = make_job(db_session, source_id=source.source_id, idempotency_key="q" * 64)
    uploading = make_job(
        db_session, source_id=source.source_id, idempotency_key="u" * 64, status=WorkUnitStatus.RUNNING, phase="upload"
    )

    with TestClient(create_app()) as client:
        response = client.post("/v1/jobs/actions", json={"action": "cancel", "job_ids": [queued.id, uploading.id]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {"success": 1, "errors": 1}
    assert {item["job_id"]: (item["status"], item["error"]) for item in payload["results"]} == {
        queued.id: ("success", None),
        uploading.id: ("error", "job_not_cancellable"),
    }
    db_session.refresh(queued)
    db_session.refresh(uploading)
    assert queued.status == WorkUnitStatus.CANCELLED
    assert (uploading.status, uploading.phase) == (WorkUnitStatus.RUNNING, "upload")
