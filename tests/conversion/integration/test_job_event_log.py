"""Integration tests for the conversion-job event log.

Crosses worker + datamodel + DB boundaries. The tests verify the
behavioral requirements from `conversion-worker` spec R1-R5 ADDED and
the MODIFIED "Transition job status atomically" requirement — every
status mutation co-commits an event row, payloads validate, subprocess
terminal events do not produce their own rows, phase persistence is
best-effort, and source-enrichment writes are audited regardless of
outcome.

After the conversion→runner cutover the status mutations are driven through
the runner adapter (:class:`~aizk.conversion.handler.ConversionStageHandler`):
``claim_next`` emits ``claimed``; ``finalize`` writes the terminal
``failed``/``cancelled`` transitions; ``recover_stale`` emits ``recovered_stale``; the
success ``upload_pending``/``succeeded`` events are written inside ``execute`` /
``_execute_upload``. The KEPT helpers ``_write_source_enrichment`` and
``record_phase_event`` are exercised directly.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import MagicMock, Mock
from uuid import uuid4

from sqlmodel import Session, select

from aizk.conversion import handler as repository_mod
from aizk.conversion.core.errors import EgressPolicyError
from aizk.conversion.datamodel.events import (
    ConversionEventKind,
    events_for_job,
    parse_payload_lenient,
)
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.processing.errors import classify_job_error
from aizk.conversion.processing.source import _write_source_enrichment
from aizk.conversion.processing.types import SourceMetaFields, SubprocessMetadata, SupervisionResult
from aizk.conversion.utilities.config import ConversionConfig
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from tests.conversion._helpers import make_job, make_source

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _events_for_job(session: Session, job_id: int) -> list[PipelineEvent]:
    """Return all conversion event rows for a job ordered by occurrence.

    Conversion's transition events now live in the shared ``pipeline_events``
    table; this delegates to the relocation-aware reader filtered to
    ``stage="conversion"`` and the job's work-unit reference.
    """
    return events_for_job(session, job_id)


def _finalize_failure(db_session: Session, job_id: int, error: Exception, config: ConversionConfig) -> None:
    """Drive a terminal FAILED write through the adapter's ``finalize``.

    Stashes the scrubbed :class:`JobErrorDetails` under the handle (the
    error-details bridge ``execute`` performs on failure) via
    ``classify_job_error``, then calls ``finalize`` with the matching FAILED
    outcome inside a caller-owned session and commits — writing the terminal
    status + ``failed`` event.
    """
    handler = ConversionStageHandler(config)
    handler._error_details[job_id] = classify_job_error(error)
    retry_class = RetryClass.RETRYABLE if bool(getattr(error, "retryable", True)) else RetryClass.PERMANENT
    with Session(db_session.get_bind()) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, retry_class))
        session.commit()


def _make_subprocess_metadata(
    *,
    karakeep_id: str,
    content_hash: str = "deadbeefcafef00d",
    source_title: str | None = "Example Post",
) -> SubprocessMetadata:
    return SubprocessMetadata(
        pipeline_name="html",
        terminal_ref={"kind": "karakeep_bookmark", "bookmark_id": karakeep_id},
        content_type="html",
        markdown_filename="output.md",
        figure_files=[],
        markdown_hash_xx64=content_hash,
        docling_version="2.0.0",
        config_snapshot={"converter_name": "docling"},
        fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        source_meta=SourceMetaFields(
            source_url="https://example.com/post",
            normalized_url="https://example.com/post",
            document_base_url=None,
            resolver_title=None,
        ),
        document_title=source_title,
        source_title=source_title,
    )


def _make_running_job(session: Session, source: Source, *, attempts: int = 1) -> ConversionJob:
    job = make_job(
        session,
        aizk_uuid=source.aizk_uuid,
        idempotency_key=uuid4().hex,
        status=ConversionJobStatus.RUNNING,
        attempts=attempts,
    )
    job.started_at = dt.datetime.now(dt.timezone.utc)
    job.source_ref = json.dumps({"kind": "karakeep_bookmark", "bookmark_id": source.karakeep_id})
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _make_queued_job(session: Session, source: Source) -> ConversionJob:
    job = make_job(
        session,
        aizk_uuid=source.aizk_uuid,
        idempotency_key=uuid4().hex,
        status=ConversionJobStatus.QUEUED,
    )
    job.source_ref = json.dumps({"kind": "karakeep_bookmark", "bookmark_id": source.karakeep_id})
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _claim_via_adapter(db_session: Session, config: ConversionConfig) -> int | None:
    """Claim the next eligible job through the adapter (emits the ``claimed`` event)."""
    handler = ConversionStageHandler(config)
    with Session(db_session.get_bind()) as session:
        claimed = handler.claim_next(session)
        session.commit()
    return claimed


# ---------------------------------------------------------------------------
# Direct-call tests — exercise individual worker functions
# ---------------------------------------------------------------------------


def test_stale_recovery_uses_recovered_stale_kind(monkeypatch, db_session: Session) -> None:
    """R4: stale-RUNNING-job sweep emits `recovered_stale` with threshold and prior started_at."""
    monkeypatch.setenv("AIZK_WORKER_STALE_JOB_MINUTES", "0")

    source = make_source(db_session, "bm_stale_event", url="https://example.com", title="Stale")
    job = _make_running_job(db_session, source, attempts=1)
    # SQLite (sa.DateTime, no timezone=True) stores datetimes as naive, so the
    # round-trip through the payload JSON also produces a naive value. Use a
    # naive timestamp here to make the comparison apples-to-apples.
    prior_started_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).replace(tzinfo=None)
    job.started_at = prior_started_at
    db_session.add(job)
    db_session.commit()

    config = ConversionConfig(_env_file=None)
    # ``recover_stale`` runs inside a caller-owned transaction (the runner owns
    # the commit); drive it through the adapter and commit here.
    handler = ConversionStageHandler(config)
    with Session(db_session.get_bind()) as session:
        recovered = handler.recover_stale(session)
        session.commit()

    assert recovered == [job.id]
    db_session.expire_all()
    refreshed = db_session.get(ConversionJob, job.id)
    assert refreshed.status == ConversionJobStatus.FAILED_RETRYABLE

    events = _events_for_job(db_session, job.id)
    assert len(events) == 1
    evt = events[0]
    assert evt.kind == ConversionEventKind.RECOVERED_STALE
    assert evt.from_status == ConversionJobStatus.RUNNING
    assert evt.to_status == ConversionJobStatus.FAILED_RETRYABLE
    assert evt.attempt == 1

    payload = parse_payload_lenient(evt.payload_json)
    assert payload.stale_after_minutes == 0
    assert payload.last_started_at is not None
    # Both sides are naive after the SQLite + JSON round-trip; small tolerance
    # for serialization rounding.
    last_naive = (
        payload.last_started_at.replace(tzinfo=None) if payload.last_started_at.tzinfo else payload.last_started_at
    )
    assert abs((last_naive - prior_started_at).total_seconds()) < 1


def test_permanent_failure_arm_emits_failed_event_with_non_retryable_indicator(
    monkeypatch, db_session: Session
) -> None:
    """R1: a non-retryable error transitions to FAILED_PERM with retryable=false."""
    source = make_source(db_session, "bm_permfail", url="https://example.com", title="Perm Fail")
    job = _make_running_job(db_session, source, attempts=1)

    config = ConversionConfig(_env_file=None)

    from aizk.conversion.core.errors import NoConverterForFormat

    err = NoConverterForFormat("no converter registered for content_type=docx")
    _finalize_failure(db_session, job.id, err, config)

    db_session.expire_all()
    refreshed = db_session.get(ConversionJob, job.id)
    assert refreshed.status == ConversionJobStatus.FAILED_PERM

    events = _events_for_job(db_session, job.id)
    failed_events = [e for e in events if e.kind == ConversionEventKind.FAILED]
    assert len(failed_events) == 1
    evt = failed_events[-1]
    assert evt.from_status == ConversionJobStatus.RUNNING
    assert evt.to_status == ConversionJobStatus.FAILED_PERM
    assert evt.attempt == 1

    payload = parse_payload_lenient(evt.payload_json)
    assert payload.retryable is False
    assert payload.error_code == err.error_code


def test_egress_policy_error_does_not_persist_destination(monkeypatch, db_session: Session) -> None:
    """R2: persisted `failed` event carries only the sanitized error_code, never the destination."""
    source = make_source(db_session, "bm_egress", url="https://example.com", title="Egress")
    job = _make_running_job(db_session, source, attempts=1)

    config = ConversionConfig(_env_file=None)

    rejected_destination = "https://attacker.example.com/exfil"
    err = EgressPolicyError(f"Egress denied to {rejected_destination}")
    err.error_code = "egress_policy_violation"
    # Simulate a traceback that mentions the destination — sanitization must drop it.
    err.traceback = f"Traceback...\nrequest_url={rejected_destination}\n"

    _finalize_failure(db_session, job.id, err, config)

    events = _events_for_job(db_session, job.id)
    failed = [e for e in events if e.kind == ConversionEventKind.FAILED][-1]
    payload = parse_payload_lenient(failed.payload_json)

    assert rejected_destination not in payload.error_message, (
        "Sanitization failed: rejected destination leaked into persisted error_message"
    )
    assert payload.error_message == payload.error_code  # bare code only
    assert payload.error_detail is None, "error_detail must be None for egress-policy failures"


def test_claim_emits_claimed_event(monkeypatch, db_session: Session) -> None:
    """The adapter's claim transitions QUEUED → RUNNING and emits `claimed`.

    The claim is the single source of the ``claimed`` event, post-incrementing
    ``attempts``.
    """
    source = make_source(db_session, "bm_claim", url="https://example.com", title="Claim")
    job = _make_queued_job(db_session, source)

    config = ConversionConfig(_env_file=None)
    claimed_id = _claim_via_adapter(db_session, config)
    assert claimed_id == job.id

    db_session.expire_all()
    refreshed = db_session.get(ConversionJob, job.id)
    assert refreshed.status == ConversionJobStatus.RUNNING
    assert refreshed.attempts == 1

    events = _events_for_job(db_session, job.id)
    assert len(events) == 1
    evt = events[0]
    assert evt.kind == ConversionEventKind.CLAIMED
    assert evt.from_status == ConversionJobStatus.QUEUED
    assert evt.to_status == ConversionJobStatus.RUNNING
    assert evt.attempt == 1  # post-increment


def test_transition_rollback_leaves_job_in_prior_status(monkeypatch, db_session: Session) -> None:
    """R1: rollback discards both the status mutation and the event row."""
    from aizk.conversion.datamodel.events import ClaimedPayload, record_transition

    source = make_source(db_session, "bm_rollback", url="https://example.com", title="Rollback")
    job = make_job(
        db_session,
        aizk_uuid=source.aizk_uuid,
        idempotency_key=uuid4().hex,
        status=ConversionJobStatus.QUEUED,
    )
    job_id = job.id

    # Open a fresh session, mutate via record_transition, then roll back.
    engine = db_session.get_bind()
    with Session(engine) as txn_session:
        txn_job = txn_session.get(ConversionJob, job_id)
        record_transition(
            txn_session,
            txn_job,
            to_status=ConversionJobStatus.RUNNING,
            kind=ConversionEventKind.CLAIMED,
            attempt=1,
            payload=ClaimedPayload(claimed_at=dt.datetime.now(dt.timezone.utc)),
        )
        txn_session.rollback()

    db_session.expire_all()
    refreshed = db_session.get(ConversionJob, job_id)
    assert refreshed.status == ConversionJobStatus.QUEUED, "Rollback must leave status unchanged"
    assert _events_for_job(db_session, job_id) == [], "Rollback must leave no event row"


def test_source_enriched_event_emitted_on_success(monkeypatch, db_session: Session) -> None:
    """R3: a successful Source UPDATE emits a `source_enriched` event with update_succeeded=True."""
    source = make_source(db_session, "bm_src_ok", url=None, title=None, content_type="html")
    job = _make_running_job(db_session, source, attempts=1)

    subprocess_meta = _make_subprocess_metadata(karakeep_id=source.karakeep_id)
    engine = db_session.get_bind()

    _write_source_enrichment(
        subprocess_meta,
        str(source.aizk_uuid),
        engine,
        job_id=job.id,
        attempt=job.attempts,
    )

    db_session.expire_all()
    events = _events_for_job(db_session, job.id)
    src_events = [e for e in events if e.kind == ConversionEventKind.SOURCE_ENRICHED]
    assert len(src_events) == 1
    payload = parse_payload_lenient(src_events[0].payload_json)
    assert payload.update_succeeded is True
    assert payload.failure_reason is None
    # Source row was updated.
    db_session.refresh(source)
    assert source.url == "https://example.com/post"
    assert source.title == "Example Post"


def test_source_enriched_event_emitted_on_failure(monkeypatch, db_session: Session) -> None:
    """R3: a failed Source UPDATE still emits a `source_enriched` event with update_succeeded=False."""
    source = make_source(db_session, "bm_src_fail", url=None, title=None, content_type="html")
    job = _make_running_job(db_session, source, attempts=1)

    subprocess_meta = _make_subprocess_metadata(karakeep_id=source.karakeep_id)
    engine = db_session.get_bind()

    # Patch Session.commit on the enrichment side to raise. The audit must
    # still record an event with update_succeeded=False.
    from sqlmodel import Session as _Session

    real_commit = _Session.commit
    raised_once = {"n": 0}

    def _flaky_commit(self):
        # Raise only on the FIRST commit (the Source UPDATE); allow the
        # subsequent event-row commit to succeed so we can verify the event.
        if raised_once["n"] == 0:
            raised_once["n"] += 1
            raise RuntimeError("simulated Source UPDATE failure")
        return real_commit(self)

    monkeypatch.setattr(_Session, "commit", _flaky_commit)

    _write_source_enrichment(
        subprocess_meta,
        str(source.aizk_uuid),
        engine,
        job_id=job.id,
        attempt=job.attempts,
    )

    monkeypatch.setattr(_Session, "commit", real_commit)

    events = _events_for_job(db_session, job.id)
    src_events = [e for e in events if e.kind == ConversionEventKind.SOURCE_ENRICHED]
    assert len(src_events) == 1
    payload = parse_payload_lenient(src_events[0].payload_json)
    assert payload.update_succeeded is False
    assert payload.failure_reason is not None


def test_direct_source_mutation_emits_no_event(db_session: Session) -> None:
    """R3: mutations to Source outside `_write_source_enrichment` produce no event row."""
    source = make_source(db_session, "bm_direct", url=None, title=None, content_type="html")

    # Direct mutation, no helper call.
    source.url = "https://example.com/direct"
    source.title = "Direct Mutation"
    db_session.add(source)
    db_session.commit()

    # No job; query the whole conversion event log.
    events = list(db_session.exec(select(PipelineEvent).where(PipelineEvent.stage == "conversion")).all())
    src_events = [e for e in events if e.kind == ConversionEventKind.SOURCE_ENRICHED]
    assert src_events == [], "Direct Source mutation must not appear as a source_enriched event"


# ---------------------------------------------------------------------------
# Full-flow tests — claim via the adapter, then drive execute with a faked
# _spawn_and_supervise that injects phase events.
# ---------------------------------------------------------------------------


def _make_fake_runtime() -> MagicMock:
    from contextlib import nullcontext

    runtime = MagicMock()
    runtime.resource_guard = nullcontext()
    runtime.capabilities.converter_requires_gpu.return_value = False
    runtime.coordinator = Mock()
    return runtime


def test_successful_job_emits_full_event_sequence(monkeypatch, db_session: Session) -> None:
    """R1+R2: end-to-end happy path emits claimed → phase × 2 → upload_pending → succeeded."""
    source = make_source(
        db_session, "bm_full_success", url="https://example.com", title="Full Success", content_type="html"
    )
    job = _make_queued_job(db_session, source)

    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    # Claim the job through the adapter so a `claimed` event is emitted.
    claimed_id = _claim_via_adapter(db_session, config)
    assert claimed_id == job.id

    # Fake spawn_and_supervise: emit phase events via the callback, write a valid
    # metadata.json into the workspace, and return a successful SupervisionResult.
    def _fake_spawn_and_supervise(**kwargs):
        on_phase = kwargs.get("on_phase_event")
        workspace = kwargs["workspace"]
        if on_phase is not None:
            on_phase("preparing_input", dt.datetime.now(dt.timezone.utc))
            on_phase("converting", dt.datetime.now(dt.timezone.utc))
        # Write a markdown file and metadata so the upload path proceeds.
        (workspace / "output.md").write_text("# hello\n")
        meta = _make_subprocess_metadata(karakeep_id=source.karakeep_id)
        (workspace / "metadata.json").write_text(meta.model_dump_json())

        class _CompletedProcess:
            pid = 9999
            exitcode = 0

            def is_alive(self) -> bool:
                return False

        return _CompletedProcess(), SupervisionResult("converting", None, False, False), None

    monkeypatch.setattr(repository_mod, "_spawn_and_supervise", _fake_spawn_and_supervise)

    # Stub out upload so we don't actually hit S3. The succeeded event is
    # emitted by `_execute_upload` so we need a real-ish stand-in.
    from aizk.conversion.datamodel.events import (
        ConversionEventKind as _Kind,
        SucceededPayload,
        record_transition,
    )

    def _fake_prepare_upload(job_id, workspace, cfg):
        return Mock(markdown_hash_xx64="deadbeefcafef00d", output_id_marker=42)

    def _fake_execute_upload(plan, job_id, cfg):
        # Mimic the real path: insert an output row, then record_transition
        # to SUCCEEDED, all in one session/commit.
        from aizk.conversion.datamodel.output import ConversionOutput

        with Session(db_session.get_bind()) as s:
            j = s.get(ConversionJob, job_id)
            if not j or j.status == ConversionJobStatus.CANCELLED:
                return
            output = ConversionOutput(
                job_id=job_id,
                aizk_uuid=j.aizk_uuid,
                owner_id=j.owner_id,
                title=j.title,
                payload_version=j.payload_version,
                s3_prefix="s3://test/",
                markdown_key=f"{j.aizk_uuid}/output.md",
                manifest_key=f"{j.aizk_uuid}/manifest.json",
                markdown_hash_xx64="deadbeefcafef00d",
                figure_count=0,
                docling_version="2.0.0",
                pipeline_name="html",
            )
            s.add(output)
            s.flush()
            j.finished_at = dt.datetime.now(dt.timezone.utc)
            j.updated_at = dt.datetime.now(dt.timezone.utc)
            record_transition(
                s,
                j,
                to_status=ConversionJobStatus.SUCCEEDED,
                kind=_Kind.SUCCEEDED,
                attempt=j.attempts,
                payload=SucceededPayload(output_id=output.id, content_hash="deadbeefcafef00d"),
            )
            s.commit()

    monkeypatch.setattr(repository_mod, "_prepare_upload", _fake_prepare_upload)
    monkeypatch.setattr(repository_mod, "_execute_upload", _fake_execute_upload)

    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    handler.execute(job.id)

    db_session.expire_all()
    refreshed = db_session.get(ConversionJob, job.id)
    assert refreshed.status == ConversionJobStatus.SUCCEEDED

    events = _events_for_job(db_session, job.id)
    kinds_in_order = [e.kind for e in events]

    # claimed, phase, phase, source_enriched (best-effort during enrichment),
    # upload_pending, succeeded.
    assert kinds_in_order[0] == ConversionEventKind.CLAIMED
    assert kinds_in_order[-2:] == [
        ConversionEventKind.UPLOAD_PENDING,
        ConversionEventKind.SUCCEEDED,
    ]
    phase_events = [e for e in events if e.kind == ConversionEventKind.PHASE]
    assert len(phase_events) >= 2, "At least two phase events for the attempt"
    for p in phase_events:
        assert p.attempt == refreshed.attempts


def test_subprocess_terminal_events_not_persisted_via_subprocess_channel(monkeypatch, db_session: Session) -> None:
    """R2: subprocess-emitted `failed`/`cancelled` reports do not produce their own event rows.

    Only the adapter's terminal write persists a `failed` event. The subprocess
    channel is real-time control, not durable storage. ``execute`` raises the
    matching conversion exception (so ``map_result``/``finalize`` classify it);
    ``finalize`` then writes the single `failed` event.
    """
    source = make_source(db_session, "bm_subterm", url="https://example.com", title="Subterm")
    job = _make_queued_job(db_session, source)

    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    claimed_id = _claim_via_adapter(db_session, config)
    assert claimed_id == job.id

    def _fake_spawn_and_supervise(**kwargs):
        # Simulate a subprocess that reported `failed` — the result carries
        # `reported_error`, which causes ``execute`` to raise ReportedChildError.
        # The subprocess `failed` event itself produces NO durable row.
        reported_error = {
            "event": "failed",
            "message": "transient timeout",
            "error_code": "timeout",
            "retryable": "true",
        }

        class _ExitedProcess:
            pid = 9998
            exitcode = 1

            def is_alive(self) -> bool:
                return False

        return _ExitedProcess(), SupervisionResult("converting", reported_error, False, False), None

    monkeypatch.setattr(repository_mod, "_spawn_and_supervise", _fake_spawn_and_supervise)

    from aizk.conversion.processing.errors import ReportedChildError

    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    # execute raises; finalize then writes the single terminal failed event.
    try:
        handler.execute(job.id)
    except ReportedChildError as exc:
        outcome = handler.map_result(exc)
        with Session(db_session.get_bind()) as session:
            handler.finalize(session, job.id, outcome)
            session.commit()
    else:  # pragma: no cover - the faked result must raise
        raise AssertionError("execute should have raised ReportedChildError")

    db_session.expire_all()
    events = _events_for_job(db_session, job.id)
    # Exactly ONE failed event — from the adapter's finalize.
    failed_events = [e for e in events if e.kind == ConversionEventKind.FAILED]
    assert len(failed_events) == 1, (
        f"Expected exactly one failed event from finalize, got {len(failed_events)}: {[e.kind for e in events]}"
    )


def test_phase_event_with_unrecognized_phase_is_dropped(monkeypatch, db_session: Session) -> None:
    """R2: a phase report whose payload fails validation is logged and dropped.

    Exercises the callback path directly: the phase callback constructs a
    `PhasePayload`; an unrecognized phase string makes `record_phase_event` log
    and return None without inserting a row.
    """
    from aizk.conversion.datamodel.events import record_phase_event

    source = make_source(db_session, "bm_bad_phase", url="https://example.com", title="Bad Phase")
    job = _make_running_job(db_session, source, attempts=1)

    engine = db_session.get_bind()
    with Session(engine) as s:
        result = record_phase_event(
            s,
            job_id=job.id,
            aizk_uuid=job.aizk_uuid,
            attempt=job.attempts,
            current_status=ConversionJobStatus.RUNNING,
            phase="not_a_real_phase",
            reported_at=dt.datetime.now(dt.timezone.utc),
        )
        s.commit()

    assert result is None

    db_session.expire_all()
    phase_events = [e for e in _events_for_job(db_session, job.id) if e.kind == ConversionEventKind.PHASE]
    assert phase_events == [], "Unrecognized phase must not produce an event row"


def test_phase_event_persistence_failure_does_not_halt_job(monkeypatch, db_session: Session) -> None:
    """R2: a failure during phase-event recording is logged; the job still proceeds.

    The adapter wraps each phase-event session in a ``try/except``. We exercise
    that wrapper by driving execute where the second phase-event invocation
    raises; the adapter must catch the error and continue, leaving the job
    projection consistent.
    """
    source = make_source(
        db_session, "bm_phase_fail", url="https://example.com", title="Phase Fail", content_type="html"
    )
    job = _make_queued_job(db_session, source)

    config = ConversionConfig(_env_file=None)
    monkeypatch.setattr(repository_mod, "get_engine", lambda _url=None: db_session.get_bind())
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    claimed_id = _claim_via_adapter(db_session, config)
    assert claimed_id == job.id

    # Replace `record_phase_event` referenced by the adapter (imported lazily
    # inside its phase callback from the events module) with a variant that
    # raises on the second invocation. The wrapper must catch the exception.
    from aizk.conversion.datamodel import events as events_mod

    real_record_phase = events_mod.record_phase_event
    call_count = {"n": 0}

    def _flaky_record_phase(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated phase-event recording failure")
        return real_record_phase(*args, **kwargs)

    monkeypatch.setattr(events_mod, "record_phase_event", _flaky_record_phase)

    def _fake_spawn_and_supervise(**kwargs):
        on_phase = kwargs.get("on_phase_event")
        workspace = kwargs["workspace"]
        if on_phase is not None:
            on_phase("preparing_input", dt.datetime.now(dt.timezone.utc))
            on_phase("converting", dt.datetime.now(dt.timezone.utc))  # raises inside the wrapper
        (workspace / "output.md").write_text("# hello\n")
        meta = _make_subprocess_metadata(karakeep_id=source.karakeep_id)
        (workspace / "metadata.json").write_text(meta.model_dump_json())

        class _CompletedProcess:
            pid = 9997
            exitcode = 0

            def is_alive(self) -> bool:
                return False

        return _CompletedProcess(), SupervisionResult("converting", None, False, False), None

    monkeypatch.setattr(repository_mod, "_spawn_and_supervise", _fake_spawn_and_supervise)
    monkeypatch.setattr(repository_mod, "_prepare_upload", lambda *a, **k: None)
    monkeypatch.setattr(repository_mod, "_execute_upload", lambda *a, **k: None)

    handler = ConversionStageHandler(config, runtime=_make_fake_runtime())
    # Must NOT raise — the wrapper swallows the second phase-event failure and continues.
    handler.execute(job.id)

    db_session.expire_all()
    refreshed = db_session.get(ConversionJob, job.id)
    # The job advanced past the phase-wiring code; it reaches at least
    # UPLOAD_PENDING (the upload stubs are no-ops, so SUCCEEDED is not asserted
    # here). What matters for R2 is that the worker did not abort.
    assert refreshed.status in {
        ConversionJobStatus.RUNNING,
        ConversionJobStatus.UPLOAD_PENDING,
    }, f"Phase-event failure must not corrupt the job projection; got {refreshed.status!r}"


def test_retryable_failure_preserves_prior_attempt_events(monkeypatch, db_session: Session) -> None:
    """R1: events from attempt 1 remain in the log when attempt 2 runs.

    Drives two terminal writes on the same job (attempt 1 retryable via finalize,
    then attempt 2 succeeds via a direct succeeded event), and asserts both
    attempts' events coexist with distinct `attempt` values.
    """
    from aizk.conversion.datamodel.events import (
        ConversionEventKind as _Kind,
        SucceededPayload,
        record_transition,
    )

    source = make_source(db_session, "bm_retry_attempts", url="https://example.com", title="Retry Attempts")
    job = _make_running_job(db_session, source, attempts=1)

    config = ConversionConfig(_env_file=None)

    # Attempt 1: retryable failure via the adapter's finalize.
    err1 = RuntimeError("transient")
    err1.error_code = "transient_failure"
    err1.retryable = True
    _finalize_failure(db_session, job.id, err1, config)

    db_session.expire_all()
    refreshed = db_session.get(ConversionJob, job.id)
    assert refreshed.status == ConversionJobStatus.FAILED_RETRYABLE
    assert refreshed.attempts == 1

    # Simulate the worker re-claiming the job for attempt 2.
    refreshed.status = ConversionJobStatus.RUNNING
    refreshed.attempts = 2
    refreshed.started_at = dt.datetime.now(dt.timezone.utc)
    db_session.add(refreshed)
    db_session.commit()

    # Attempt 2 succeeds — emit a succeeded event directly.
    engine = db_session.get_bind()
    with Session(engine) as s:
        j = s.get(ConversionJob, job.id)
        record_transition(
            s,
            j,
            to_status=ConversionJobStatus.SUCCEEDED,
            kind=_Kind.SUCCEEDED,
            attempt=j.attempts,
            payload=SucceededPayload(output_id=123, content_hash="cafef00d"),
        )
        s.commit()

    events = _events_for_job(db_session, job.id)
    # We must see at least: failed(attempt=1), succeeded(attempt=2).
    failed_attempt1 = [e for e in events if e.kind == _Kind.FAILED and e.attempt == 1]
    succeeded_attempt2 = [e for e in events if e.kind == _Kind.SUCCEEDED and e.attempt == 2]
    assert len(failed_attempt1) == 1, "Attempt-1 failed event must remain in the log"
    assert len(succeeded_attempt2) == 1, "Attempt-2 succeeded event must coexist"
    # Confirm the attempt-1 event kind is `failed`, not `recovered_stale`.
    assert failed_attempt1[0].kind == _Kind.FAILED
