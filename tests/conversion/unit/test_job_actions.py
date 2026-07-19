"""Direct tests for the lifted conversion job-action domain helpers.

These pin the domain contract of ``aizk.conversion.job_actions`` independently of
any HTTP surface, so the helpers stay covered when the conversion HTML UI (their
only caller for delete today) is removed. Delete is the exposed one: it is a hard
removal of the job and its ``ConversionOutput`` with no lifecycle event, gated on a
terminal-status eligibility set.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, select

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.job_actions import (
    DELETABLE_STATUSES,
    apply_job_cancel,
    apply_job_delete,
    apply_job_retry,
)
from tests.conversion._helpers import make_job, make_source


def _utcnow() -> dt.datetime:
    """Return a timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)


def _make_output(session: Session, *, job_id: int, source_id) -> ConversionOutput:
    """Insert a minimal ``ConversionOutput`` markdown locator for a job."""
    output = ConversionOutput(
        job_id=job_id,
        source_id=source_id,
        owner_id="self",
        title="Output",
        payload_version=1,
        s3_prefix="conv/test",
        markdown_key="markdown.md",
        manifest_key="manifest.json",
        markdown_hash_xx64="0" * 16,
        docling_version="0.0.0",
        pipeline_name="test",
    )
    session.add(output)
    session.commit()
    return output


def test_deletable_statuses_are_the_terminal_non_active_set() -> None:
    """Delete is eligible exactly from the terminal, non-active statuses."""
    assert (
        frozenset(
            {
                ConversionJobStatus.FAILED_RETRYABLE,
                ConversionJobStatus.FAILED_PERM,
                ConversionJobStatus.CANCELLED,
            }
        )
        == DELETABLE_STATUSES
    )


def test_apply_job_delete_removes_the_job_and_its_output(db_session: Session) -> None:
    """Delete removes the job row and its conversion output; the job id is gone."""
    source = make_source(db_session, "bm_delete")
    job = make_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="d" * 64,
        status=ConversionJobStatus.FAILED_PERM,
    )
    _make_output(db_session, job_id=job.id, source_id=source.source_id)
    job_id = job.id

    apply_job_delete(db_session, job)
    db_session.commit()

    assert db_session.get(ConversionJob, job_id) is None
    assert db_session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).first() is None


def test_apply_job_delete_rejects_a_non_terminal_job(db_session: Session) -> None:
    """A running job is not deletable; the helper raises without mutating."""
    source = make_source(db_session, "bm_delete_running")
    job = make_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="r" * 64,
        status=ConversionJobStatus.RUNNING,
    )

    with pytest.raises(ValueError, match="job_not_deletable"):
        apply_job_delete(db_session, job)

    db_session.refresh(job)
    assert db_session.get(ConversionJob, job.id) is not None
    assert job.status is ConversionJobStatus.RUNNING


def test_apply_job_retry_requeues_and_increments_attempt(db_session: Session) -> None:
    """Retry re-queues a failed job, increments the attempt, and clears the error."""
    source = make_source(db_session, "bm_retry")
    job = make_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="y" * 64,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        attempts=1,
    )

    apply_job_retry(db_session, job, _utcnow(), submitted_by="self")
    db_session.commit()
    db_session.refresh(job)

    assert job.status is ConversionJobStatus.QUEUED
    assert job.attempts == 2
    assert job.error_code is None
    assert job.earliest_next_attempt_at is None


def test_apply_job_cancel_terminates_an_active_job(db_session: Session) -> None:
    """Cancel writes a terminal ``CANCELLED`` status for a queued job."""
    source = make_source(db_session, "bm_cancel")
    job = make_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="c" * 64,
        status=ConversionJobStatus.QUEUED,
    )

    apply_job_cancel(db_session, job, _utcnow(), cancelled_by="self")
    db_session.commit()
    db_session.refresh(job)

    assert job.status is ConversionJobStatus.CANCELLED
    assert job.finished_at is not None


def test_apply_job_cancel_rejects_a_succeeded_job(db_session: Session) -> None:
    """A succeeded job is not cancellable; the helper raises without mutating."""
    source = make_source(db_session, "bm_cancel_done")
    job = make_job(
        db_session,
        source_id=source.source_id,
        idempotency_key="s" * 64,
        status=ConversionJobStatus.SUCCEEDED,
    )

    with pytest.raises(ValueError, match="job_not_cancellable"):
        apply_job_cancel(db_session, job, _utcnow(), cancelled_by="self")

    db_session.refresh(job)
    assert job.status is ConversionJobStatus.SUCCEEDED
