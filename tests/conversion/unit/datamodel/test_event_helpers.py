"""Unit tests for ``record_transition`` / ``record_phase_event`` / ``record_source_event``.

These helpers enforce the calling conventions documented in
``aizk.conversion.datamodel.events`` (and ``design.md § HelperCallingConventions``):

- The helper does NOT commit; the caller owns the transaction boundary.
- ``attempt`` is an explicit required parameter; the helper never infers it
  from ``job.attempts``.
- Validation failure on the typed payload raises BEFORE ``job.status`` is
  mutated, so partial state never leaks past a failed write.
- ``record_phase_event`` is best-effort: validation or persistence failure
  is logged and swallowed without halting the job.

Tests use SQLite in-memory via ``SQLModel.metadata.create_all`` so they do
not depend on Alembic migrations being applied.
"""

from __future__ import annotations

import datetime
import logging
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel, select

import aizk.conversion.datamodel  # noqa: F401  (registers SQLModel metadata)
from aizk.conversion.datamodel.events import (
    STAGE,
    ClaimedPayload,
    ConversionEventKind,
    FailedPayload,
    PhasePayload,
    QueuedPayload,
    record_phase_event,
    record_source_event,
    record_transition,
)
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source
from aizk.pipeline.events import PipelineEvent

_NOW = datetime.datetime(2026, 5, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _conversion_events(session) -> list[PipelineEvent]:
    """Return all conversion-stage events in insertion order."""
    return session.exec(
        select(PipelineEvent).where(PipelineEvent.stage == STAGE).order_by(PipelineEvent.event_id)
    ).all()


@pytest.fixture()
def engine(tmp_path):
    """Provide a fresh in-memory SQLite engine with the full schema."""
    url = f"sqlite:///{tmp_path}/events.db"
    eng = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture()
def source(engine) -> Source:
    """Persist a Source row and return it."""
    with Session(engine) as session:
        src = Source(
            karakeep_id=f"k_{uuid4().hex[:8]}",
            source_ref='{"kind":"karakeep_bookmark","bookmark_id":"x"}',
            source_ref_hash=uuid4().hex,
            owner_id="self",
        )
        session.add(src)
        session.commit()
        session.refresh(src)
        return src


@pytest.fixture()
def queued_job(engine, source) -> ConversionJob:
    """Persist a ConversionJob in QUEUED with attempts=0."""
    with Session(engine) as session:
        job = ConversionJob(
            source_id=source.source_id,
            owner_id="self",
            title="test",
            payload_version=1,
            status=ConversionJobStatus.QUEUED,
            attempts=0,
            idempotency_key=uuid4().hex,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


# ---------------------------------------------------------------------------
# record_transition: commit ownership and transaction shape
# ---------------------------------------------------------------------------


def test_record_transition_does_not_commit(engine, queued_job):
    """Helper must not call ``session.commit()`` — caller owns boundaries."""
    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        record_transition(
            session,
            job,
            to_status=ConversionJobStatus.RUNNING,
            kind=ConversionEventKind.CLAIMED,
            attempt=1,
            payload=ClaimedPayload(claimed_at=_NOW, worker_pid=1),
        )
        # Helper staged via session.add but did NOT commit. A separate
        # session should still see the pre-mutation state.
        with Session(engine) as other:
            other_job = other.get(ConversionJob, queued_job.id)
            assert other_job.status == ConversionJobStatus.QUEUED
            assert _conversion_events(other) == []

        session.commit()

    # After commit both rows are observable.
    with Session(engine) as verify:
        committed = verify.get(ConversionJob, queued_job.id)
        assert committed.status == ConversionJobStatus.RUNNING
        events = _conversion_events(verify)
        assert len(events) == 1
        assert events[0].kind == ConversionEventKind.CLAIMED.value
        assert events[0].attempt == 1
        # Relocation: the event lands in pipeline_events with the generic
        # work-unit reference (the job id rendered to text) and the stage tag.
        assert events[0].stage == STAGE
        assert events[0].work_unit_ref == str(queued_job.id)


def test_record_transition_writes_both_rows_in_one_commit(engine, queued_job):
    """Status mutation and event row land atomically on commit."""
    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        record_transition(
            session,
            job,
            to_status=ConversionJobStatus.RUNNING,
            kind=ConversionEventKind.CLAIMED,
            attempt=1,
            payload=ClaimedPayload(claimed_at=_NOW),
        )
        session.commit()

    with Session(engine) as verify:
        committed = verify.get(ConversionJob, queued_job.id)
        events = _conversion_events(verify)
        assert committed.status == ConversionJobStatus.RUNNING
        assert len(events) == 1
        assert events[0].from_status == ConversionJobStatus.QUEUED.value
        assert events[0].to_status == ConversionJobStatus.RUNNING.value


def test_record_transition_persists_event_kind_value(engine, queued_job):
    """Raw database rows store ``ConversionEventKind.value``, not enum member names."""
    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        record_transition(
            session,
            job,
            to_status=ConversionJobStatus.RUNNING,
            kind=ConversionEventKind.CLAIMED,
            attempt=1,
            payload=ClaimedPayload(claimed_at=_NOW),
        )
        session.commit()

    with Session(engine) as verify:
        row = verify.execute(text("SELECT kind FROM pipeline_events WHERE stage = 'conversion'")).one()
        assert row[0] == ConversionEventKind.CLAIMED.value


def test_record_transition_rolled_back_transaction_discards_both(engine, queued_job):
    """Rollback discards both the job mutation and the event row."""
    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        record_transition(
            session,
            job,
            to_status=ConversionJobStatus.RUNNING,
            kind=ConversionEventKind.CLAIMED,
            attempt=1,
            payload=ClaimedPayload(claimed_at=_NOW),
        )
        session.rollback()

    with Session(engine) as verify:
        committed = verify.get(ConversionJob, queued_job.id)
        events = _conversion_events(verify)
        assert committed.status == ConversionJobStatus.QUEUED
        assert events == []


# ---------------------------------------------------------------------------
# record_transition: validation and required arguments
# ---------------------------------------------------------------------------


def test_record_transition_validates_payload_before_mutation(engine, queued_job):
    """A payload/kind mismatch must raise BEFORE ``job.status`` is mutated."""
    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        with pytest.raises(ValueError):
            record_transition(
                session,
                job,
                to_status=ConversionJobStatus.RUNNING,
                kind=ConversionEventKind.FAILED,  # mismatch with claimed payload
                attempt=1,
                payload=ClaimedPayload(claimed_at=_NOW),
            )
        assert job.status == ConversionJobStatus.QUEUED


def test_record_transition_attempt_is_required(engine, queued_job):
    """Calling without explicit ``attempt`` raises ``TypeError``."""
    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        with pytest.raises(TypeError):
            record_transition(
                session,
                job,
                to_status=ConversionJobStatus.RUNNING,
                kind=ConversionEventKind.CLAIMED,
                payload=ClaimedPayload(claimed_at=_NOW),  # missing attempt=
            )


def test_record_transition_unknown_kind_payload_raises(engine, queued_job):
    """Constructing a payload with an extra field raises pydantic ValidationError."""
    from pydantic import ValidationError

    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        with pytest.raises(ValidationError):
            record_transition(
                session,
                job,
                to_status=ConversionJobStatus.FAILED_PERM,
                kind=ConversionEventKind.FAILED,
                attempt=1,
                payload=FailedPayload(
                    error_code="x",
                    error_message="y",
                    retryable=False,
                    bogus_extra_field="nope",  # type: ignore[call-arg]
                ),
            )
        assert job.status == ConversionJobStatus.QUEUED


def test_record_transition_revalidates_payload_model_shape(engine, queued_job):
    """A foreign BaseModel with matching kind still must satisfy the typed union."""
    from pydantic import ValidationError, create_model

    malformed_failed_payload = create_model(
        "MalformedFailedPayload",
        kind=(str, "failed"),
        bogus=(str, "nope"),
    )

    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        with pytest.raises(ValidationError):
            record_transition(
                session,
                job,
                to_status=ConversionJobStatus.FAILED_PERM,
                kind=ConversionEventKind.FAILED,
                attempt=1,
                payload=malformed_failed_payload(),
            )
        assert job.status == ConversionJobStatus.QUEUED
        assert _conversion_events(session) == []


# ---------------------------------------------------------------------------
# record_phase_event
# ---------------------------------------------------------------------------


def test_record_phase_event_does_not_mutate_status(engine, queued_job):
    """Phase events describe progress within RUNNING — no status mutation."""
    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        # Move to RUNNING first for realism.
        job.status = ConversionJobStatus.RUNNING
        session.add(job)
        session.commit()

        record_phase_event(
            session,
            job_id=job.id,
            source_id=job.source_id,
            attempt=1,
            current_status=ConversionJobStatus.RUNNING,
            phase="converting",
            reported_at=_NOW,
        )
        session.commit()

    with Session(engine) as verify:
        committed = verify.get(ConversionJob, queued_job.id)
        events = _conversion_events(verify)
        assert committed.status == ConversionJobStatus.RUNNING
        assert len(events) == 1
        assert events[0].kind == ConversionEventKind.PHASE.value
        assert events[0].from_status == ConversionJobStatus.RUNNING.value
        assert events[0].to_status == ConversionJobStatus.RUNNING.value


def test_record_phase_event_persistence_failure_is_swallowed_and_logged(engine, queued_job, monkeypatch, caplog):
    """A persistence-side failure must be logged and swallowed, not raised."""

    class _RaisingSession:
        def add(self, _obj):
            raise RuntimeError("simulated session.add failure")

    with caplog.at_level(logging.WARNING, logger="aizk.conversion.datamodel.events"):
        result = record_phase_event(
            _RaisingSession(),
            job_id=queued_job.id,
            source_id=queued_job.source_id,
            attempt=1,
            current_status=ConversionJobStatus.RUNNING,
            phase="converting",
            reported_at=_NOW,
        )

    assert result is None
    assert any("Phase event persistence failed" in r.message for r in caplog.records)


def test_record_phase_event_validation_failure_drops_row(engine, queued_job, caplog):
    """An unrecognized phase string fails validation; nothing is inserted."""
    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)

        with caplog.at_level(logging.WARNING, logger="aizk.conversion.datamodel.events"):
            result = record_phase_event(
                session,
                job_id=job.id,
                source_id=job.source_id,
                attempt=1,
                current_status=ConversionJobStatus.RUNNING,
                phase="not_a_real_phase",
                reported_at=_NOW,
            )
        session.commit()

    assert result is None
    assert any("validation failure" in r.message for r in caplog.records)

    with Session(engine) as verify:
        events = _conversion_events(verify)
        assert events == []


# ---------------------------------------------------------------------------
# record_source_event
# ---------------------------------------------------------------------------


def test_record_source_event_does_not_commit(engine, queued_job):
    """Source-enrichment helper stages but does not commit."""
    with Session(engine) as session:
        record_source_event(
            session,
            job_id=queued_job.id,
            source_id=queued_job.source_id,
            attempt=1,
            columns_written=["url", "title"],
            update_succeeded=True,
            failure_reason=None,
        )
        with Session(engine) as other:
            assert _conversion_events(other) == []
        session.commit()

    with Session(engine) as verify:
        events = _conversion_events(verify)
        assert len(events) == 1
        assert events[0].kind == ConversionEventKind.SOURCE_ENRICHED.value


def test_record_source_event_failure_indicator(engine, queued_job):
    """Source-enrichment event records the update_succeeded=False outcome."""
    with Session(engine) as session:
        record_source_event(
            session,
            job_id=queued_job.id,
            source_id=queued_job.source_id,
            attempt=1,
            columns_written=["url", "title"],
            update_succeeded=False,
            failure_reason="transient db error",
        )
        session.commit()

    with Session(engine) as verify:
        events = _conversion_events(verify)
        assert len(events) == 1
        # Decode payload — it carries the failure indicator.
        import json

        payload = json.loads(events[0].payload_json)
        assert payload["update_succeeded"] is False
        assert payload["failure_reason"] == "transient db error"


# ---------------------------------------------------------------------------
# Initial-submission shape: from_status NULL via explicit override
# ---------------------------------------------------------------------------


def test_initial_submission_event_writes_null_from_status(engine, source):
    """Origin events written with from_status=None persist NULL prior status.

    Spec R1 scenario "Initial submission event has no prior status"
    requires the first event for a brand-new job to carry NULL
    from_status. The API submit path constructs the ConversionJob with
    status=QUEUED already, so without the override the helper would
    record from_status=QUEUED. Passing from_status=None explicitly is
    how the origin-event shape is achieved.
    """
    with Session(engine) as session:
        job = ConversionJob(
            source_id=source.source_id,
            owner_id="self",
            title="test",
            payload_version=1,
            status=ConversionJobStatus.QUEUED,
            attempts=0,
            idempotency_key=uuid4().hex,
        )
        session.add(job)
        session.flush()  # need job.id for the event row

        record_transition(
            session,
            job,
            to_status=ConversionJobStatus.QUEUED,
            kind=ConversionEventKind.QUEUED,
            attempt=0,
            payload=QueuedPayload(submitted_by="user-1", requeue_reason="initial"),
            from_status=None,
        )
        session.commit()

    with Session(engine) as verify:
        events = _conversion_events(verify)
        assert len(events) == 1
        assert events[0].kind == ConversionEventKind.QUEUED.value
        assert events[0].from_status is None, (
            "Origin event must persist NULL from_status when from_status=None is passed"
        )
        assert events[0].to_status == ConversionJobStatus.QUEUED.value
        assert events[0].attempt == 0


def test_record_transition_from_status_defaults_to_job_status(engine, queued_job):
    """Without an override, from_status is read from job.status before mutation."""
    with Session(engine) as session:
        job = session.get(ConversionJob, queued_job.id)
        record_transition(
            session,
            job,
            to_status=ConversionJobStatus.RUNNING,
            kind=ConversionEventKind.CLAIMED,
            attempt=1,
            payload=ClaimedPayload(claimed_at=_NOW),
        )
        session.commit()

    with Session(engine) as verify:
        events = _conversion_events(verify)
        assert events[0].from_status == ConversionJobStatus.QUEUED.value, (
            "Default behavior must read from_status from job.status before mutation"
        )
