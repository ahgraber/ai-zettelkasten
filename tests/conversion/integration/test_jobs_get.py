"""Integration tests for the ``retry_class`` and ``phase`` fields of GET /v1/jobs/{job_id}."""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from aizk.conversion.api.main import create_app
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.utilities.clock import utcnow
from tests.conversion._helpers import make_job, make_source


@pytest.mark.parametrize(
    ("status", "has_wait", "retry_class"),
    [
        (WorkUnitStatus.FAILED, True, "retryable"),
        (WorkUnitStatus.FAILED, False, "permanent"),
        (WorkUnitStatus.QUEUED, False, None),
        (WorkUnitStatus.RUNNING, False, None),
        (WorkUnitStatus.SUCCEEDED, False, None),
        (WorkUnitStatus.CANCELLED, False, None),
        (WorkUnitStatus.TIMED_OUT, False, None),
    ],
)
def test_get_reports_the_retry_class_of_a_failed_job_only(
    db_session, status: WorkUnitStatus, has_wait: bool, retry_class: str | None
) -> None:
    """A failed job is ``retryable`` with a retry wait and ``permanent`` without one; every other status has none."""
    source = make_source(db_session, "bm_get_retry_class")
    job = make_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="r" * 64,
        status=status,
        earliest_next_attempt_at=utcnow() if has_wait else None,
    )

    with TestClient(create_app()) as client:
        response = client.get(f"/v1/jobs/{job.id}")

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["retry_class"]) == (status.value, retry_class)


@pytest.mark.parametrize(
    ("status", "stored_phase", "phase"),
    [
        (WorkUnitStatus.RUNNING, "upload", "upload"),
        (WorkUnitStatus.RUNNING, "convert", "convert"),
        (WorkUnitStatus.FAILED, "upload", None),
    ],
)
def test_get_reports_the_phase_only_while_running(
    db_session, status: WorkUnitStatus, stored_phase: str, phase: str | None
) -> None:
    """``phase`` is the stored phase while the job is ``running`` and ``null`` in any other status."""
    source = make_source(db_session, "bm_get_phase")
    job = make_job(db_session, source_id=source.source_id, idempotency_key="p" * 64, status=status, phase=stored_phase)

    with TestClient(create_app()) as client:
        response = client.get(f"/v1/jobs/{job.id}")

    assert response.status_code == 200
    assert response.json()["phase"] == phase
