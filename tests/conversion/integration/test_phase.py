"""Integration tests for the conversion job's ``phase`` across claim, upload, retry, and cancel."""

from __future__ import annotations

import datetime as dt

from pyleak import no_thread_leaks
from sqlmodel import Session, select

from aizk.conversion import handler as handler_mod
from aizk.conversion.datamodel.events import ConversionEventKind, events_for_job
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.settings import ConversionConfig
from aizk.conversion.stage import cancel_conversion_job, retry_conversion_job
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.repository import IneligibleTransitionError
from aizk.storage.s3 import S3UploadError
from aizk.utilities.clock import utcnow
from tests.conversion._helpers import conversion_runner, convert_inline, make_queued_conversion
from tests.fakes.storage import install_spy_s3


def _phase(engine, job_id: int) -> str | None:
    with Session(engine) as session:
        return session.get(ConversionJob, job_id).phase


def _outputs(engine, job_id: int) -> list[ConversionOutput]:
    with Session(engine) as session:
        return list(session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).all())


def _sweep_and_reclaim(engine, config: ConversionConfig, job_id: int) -> None:
    other = ConversionStageHandler(config)
    with Session(engine) as session:
        stranded = session.get(ConversionJob, job_id)
        stranded.started_at = utcnow() - dt.timedelta(minutes=60)
        session.add(stranded)
        session.commit()
        assert other.recover_stale(session) == [job_id]
        session.commit()
        assert other.claim_next(session) == job_id
        session.commit()


def test_a_conversion_outliving_its_claim_writes_no_upload_phase(monkeypatch, db_session: Session) -> None:
    """A stale sweep and a second claim during the conversion leave the first attempt no phase write, event, or output."""
    engine = db_session.get_bind()
    config = ConversionConfig(_env_file=None, worker_stale_job_minutes=30)
    job = make_queued_conversion(db_session, "bm_phase_outlived")
    convert_inline(monkeypatch, on_convert=lambda job_id: _sweep_and_reclaim(engine, config, job_id))
    spy = install_spy_s3(monkeypatch)
    runner = conversion_runner(engine, config)

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    with Session(engine) as session:
        row = session.get(ConversionJob, job.id)
        assert (row.status, row.attempts, row.phase) == (WorkUnitStatus.RUNNING, 2, "convert")
        kinds = [event.kind for event in events_for_job(session, job.id)]
    assert ConversionEventKind.CONVERSION_COMPLETED not in kinds
    assert _outputs(engine, job.id) == []
    assert spy.puts == []
    counters = runner.metrics.counters
    assert "pipeline.work_unit.succeeded" not in counters
    assert "pipeline.work_unit.cancelled" not in counters
    assert counters.get("pipeline.work_unit.finalize_skipped") == 1


def test_a_conversion_outliving_its_claim_writes_no_enrichment_or_phase_event(
    monkeypatch, db_session: Session
) -> None:
    """After another claim takes the job mid-conversion, the first attempt leaves the Source row and the event log alone."""
    engine = db_session.get_bind()
    config = ConversionConfig(_env_file=None, worker_stale_job_minutes=30)
    job = make_queued_conversion(db_session, "bm_phase_unenriched")
    with Session(engine) as session:
        before = session.exec(select(Source).where(Source.source_id == job.source_id)).one().model_dump()
    convert_inline(monkeypatch, on_convert=lambda job_id: _sweep_and_reclaim(engine, config, job_id))
    install_spy_s3(monkeypatch)

    with no_thread_leaks(action="raise"):
        conversion_runner(engine, config).run_until_idle()

    with Session(engine) as session:
        assert session.exec(select(Source).where(Source.source_id == job.source_id)).one().model_dump() == before
        kinds = [event.kind for event in events_for_job(session, job.id)]
    assert ConversionEventKind.SOURCE_ENRICHED not in kinds
    assert ConversionEventKind.PHASE not in kinds


def test_the_phase_reads_convert_then_upload_then_nothing(monkeypatch, db_session: Session) -> None:
    """A claimed job reads ``convert`` while converting, ``upload`` at every PUT, and no phase once it succeeds."""
    engine = db_session.get_bind()
    seen: list[tuple[str, str | None]] = []
    convert_inline(monkeypatch, on_convert=lambda job_id: seen.append(("convert", _phase(engine, job_id))))
    spy = install_spy_s3(monkeypatch)
    job = make_queued_conversion(db_session, "bm_phase_flow")
    spy.before_put = lambda _key: seen.append(("put", _phase(engine, job.id)))

    with no_thread_leaks(action="raise"):
        conversion_runner(engine, ConversionConfig(_env_file=None)).run_until_idle()

    assert seen == [("convert", "convert"), ("put", "upload"), ("put", "upload")]
    with Session(engine) as session:
        done = session.get(ConversionJob, job.id)
        assert (done.status, done.phase) == (WorkUnitStatus.SUCCEEDED, None)


def test_a_job_retried_after_an_upload_failure_is_reclaimed_in_the_convert_phase(
    monkeypatch, db_session: Session
) -> None:
    """After an upload failure clears the phase and an operator retries, the next claim reads ``convert``."""
    engine = db_session.get_bind()
    phases: list[str | None] = []
    convert_inline(monkeypatch, on_convert=lambda job_id: phases.append(_phase(engine, job_id)))
    spy = install_spy_s3(monkeypatch)

    def _refuse(key: str) -> None:
        raise S3UploadError(key, "connection reset")

    spy.before_put = _refuse
    job = make_queued_conversion(db_session, "bm_phase_retry")
    config = ConversionConfig(_env_file=None, retry_max_attempts=1, retry_base_delay_seconds=3600)

    with no_thread_leaks(action="raise"):
        conversion_runner(engine, config).run_until_idle()

    with Session(engine) as session:
        failed = session.get(ConversionJob, job.id)
        assert (failed.status, failed.phase) == (WorkUnitStatus.FAILED, None)
        retry_conversion_job(session, failed, principal="self")
        session.commit()
    spy.before_put = lambda _key: None

    with no_thread_leaks(action="raise"):
        conversion_runner(engine, config).run_until_idle()

    assert phases == ["convert", "convert"]
    with Session(engine) as session:
        assert session.get(ConversionJob, job.id).status is WorkUnitStatus.SUCCEEDED


def test_a_cancel_committed_during_convert_stops_the_upload_phase_write(monkeypatch, db_session: Session) -> None:
    """A cancel landing after the cancel poll and before the ``upload`` write ends the job ``cancelled`` with nothing uploaded."""
    engine = db_session.get_bind()
    convert_inline(monkeypatch)
    spy = install_spy_s3(monkeypatch)
    job = make_queued_conversion(db_session, "bm_phase_cancelled")
    enrich = handler_mod._write_source_enrichment

    def _cancel_then_enrich(*args, **kwargs):
        with Session(engine) as session:
            cancel_conversion_job(session, session.get(ConversionJob, job.id), principal="self")
            session.commit()
        return enrich(*args, **kwargs)

    monkeypatch.setattr(handler_mod, "_write_source_enrichment", _cancel_then_enrich)
    runner = conversion_runner(engine, ConversionConfig(_env_file=None))

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    with Session(engine) as session:
        cancelled = session.get(ConversionJob, job.id)
        assert (cancelled.status, cancelled.phase) == (WorkUnitStatus.CANCELLED, None)
        kinds = [event.kind for event in events_for_job(session, job.id)]
    assert ConversionEventKind.CONVERSION_COMPLETED not in kinds
    assert kinds.count(ConversionEventKind.CANCELLED) == 1
    assert _outputs(engine, job.id) == []
    assert spy.puts == []
    assert spy.blobs == {}
    counters = runner.metrics.counters
    assert "pipeline.work_unit.succeeded" not in counters
    assert counters.get("pipeline.work_unit.finalize_skipped") == 1
    assert runner.metrics.counter_tags["pipeline.work_unit.finalize_skipped"]["outcome"] == "cancelled"


def test_a_cancel_requested_during_upload_is_refused_and_the_job_succeeds(monkeypatch, db_session: Session) -> None:
    """Every cancel requested while the job is in its upload phase is refused; the job succeeds with its output row."""
    engine = db_session.get_bind()
    convert_inline(monkeypatch)
    spy = install_spy_s3(monkeypatch)
    job = make_queued_conversion(db_session, "bm_phase_uncancellable")
    refusals: list[str] = []

    def _try_cancel(_key: str) -> None:
        with Session(engine) as session:
            try:
                cancel_conversion_job(session, session.get(ConversionJob, job.id), principal="self")
            except IneligibleTransitionError as exc:
                refusals.append(exc.code)
            session.commit()

    spy.before_put = _try_cancel

    with no_thread_leaks(action="raise"):
        conversion_runner(engine, ConversionConfig(_env_file=None)).run_until_idle()

    assert refusals == ["job_not_cancellable", "job_not_cancellable"]
    with Session(engine) as session:
        done = session.get(ConversionJob, job.id)
        assert (done.status, done.phase) == (WorkUnitStatus.SUCCEEDED, None)
    assert len(_outputs(engine, job.id)) == 1
