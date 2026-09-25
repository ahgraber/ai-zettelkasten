"""Integration tests for the conversion upload: success after verified PUTs, failures, reconversion, and recovery."""

from __future__ import annotations

import datetime as dt

from pyleak import no_thread_leaks
from sqlmodel import Session, select

from aizk.conversion.datamodel.events import ConversionEventKind, events_for_job
from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.settings import ConversionConfig
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.storage.s3 import S3UploadError
from aizk.utilities.clock import utcnow
from tests.conversion._helpers import conversion_runner, convert_inline, make_queued_conversion
from tests.fakes.storage import install_spy_s3


def _refuse(tries: list[str]):
    def _put(key: str) -> None:
        tries.append(key)
        raise S3UploadError(key, "connection reset")

    return _put


def _outputs(engine, job_id: int) -> list[ConversionOutput]:
    with Session(engine) as session:
        return list(session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).all())


def test_a_verified_upload_succeeds_only_after_every_put(monkeypatch, db_session: Session) -> None:
    """The job reads ``running`` at every PUT and ``succeeded`` after them, and the runner counts one success."""
    engine = db_session.get_bind()
    convert_inline(monkeypatch)
    spy = install_spy_s3(monkeypatch)
    job = make_queued_conversion(db_session, "bm_upload_verified")

    statuses: list[WorkUnitStatus] = []

    def _read_status(_key: str) -> None:
        with Session(engine) as session:
            statuses.append(WorkUnitStatus(session.get(ConversionJob, job.id).status))

    spy.before_put = _read_status
    runner = conversion_runner(engine, ConversionConfig(_env_file=None))
    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    assert statuses == [WorkUnitStatus.RUNNING, WorkUnitStatus.RUNNING]
    with Session(engine) as session:
        assert session.get(ConversionJob, job.id).status is WorkUnitStatus.SUCCEEDED
    [output] = _outputs(engine, job.id)
    assert spy.puts == [output.markdown_key, output.manifest_key]
    assert runner.metrics.counters.get("pipeline.work_unit.succeeded") == 1


def test_an_exhausted_upload_failure_waits_and_the_next_attempt_converts_again(
    monkeypatch, db_session: Session
) -> None:
    """Upload retries exhausted leave the job ``failed`` with a retry wait; the next attempt converts before uploading."""
    engine = db_session.get_bind()
    config = ConversionConfig(_env_file=None, retry_max_attempts=2, retry_base_delay_seconds=0)
    first = conversion_runner(engine, config)
    converted = convert_inline(monkeypatch, on_convert=lambda _job_id: first.shutdown.request_shutdown())
    spy = install_spy_s3(monkeypatch)
    tries: list[str] = []
    spy.before_put = _refuse(tries)
    job = make_queued_conversion(db_session, "bm_upload_exhausted")

    with no_thread_leaks(action="raise"):
        first.run_until_idle()

    with Session(engine) as session:
        failed = session.get(ConversionJob, job.id)
        assert failed.status is WorkUnitStatus.FAILED
        assert failed.earliest_next_attempt_at is not None
        assert failed.finished_at is None
        assert failed.phase is None
    assert len(tries) == 2, "the upload's own retries ran"

    spy.before_put = lambda _key: None
    with no_thread_leaks(action="raise"):
        conversion_runner(engine, config).run_until_idle()

    assert converted == [job.id, job.id]
    with Session(engine) as session:
        succeeded = session.get(ConversionJob, job.id)
        assert (succeeded.status, succeeded.attempts) == (WorkUnitStatus.SUCCEEDED, 2)


def test_a_failed_upload_publishes_no_artifact_paths(monkeypatch, db_session: Session) -> None:
    """A final PUT that fails after an earlier PUT stored its object leaves no output row and no success."""
    engine = db_session.get_bind()
    convert_inline(monkeypatch)
    spy = install_spy_s3(monkeypatch)

    def _refuse_manifest(key: str) -> None:
        if key.endswith("manifest.json"):
            raise S3UploadError(key, "checksum mismatch")

    spy.before_put = _refuse_manifest
    job = make_queued_conversion(db_session, "bm_upload_unverified")
    config = ConversionConfig(_env_file=None, retry_max_attempts=1, retry_base_delay_seconds=3600)

    with no_thread_leaks(action="raise"):
        conversion_runner(engine, config).run_until_idle()

    assert len(spy.puts) == 1, "the markdown object was stored before the manifest PUT failed"
    assert _outputs(engine, job.id) == []
    with Session(engine) as session:
        assert session.get(ConversionJob, job.id).status is WorkUnitStatus.FAILED
        kinds = [event.kind for event in events_for_job(session, job.id)]
    assert ConversionEventKind.SUCCEEDED not in kinds


def test_a_reconverted_job_overwrites_the_same_keys(monkeypatch, db_session: Session) -> None:
    """The attempt after an upload failure PUTs to the keys the failed attempt wrote, leaving no other object."""
    engine = db_session.get_bind()
    converted = convert_inline(monkeypatch)
    spy = install_spy_s3(monkeypatch)
    first_attempt_puts: list[str] = []

    def _refuse_first_manifest(key: str) -> None:
        if len(converted) == 1 and key.endswith("manifest.json"):
            first_attempt_puts.extend(spy.puts)
            raise S3UploadError(key, "connection reset")

    spy.before_put = _refuse_first_manifest
    job = make_queued_conversion(db_session, "bm_upload_reconverted")
    config = ConversionConfig(_env_file=None, retry_max_attempts=1, retry_base_delay_seconds=0)

    with no_thread_leaks(action="raise"):
        conversion_runner(engine, config).run_until_idle()

    assert converted == [job.id, job.id]
    second_attempt_puts = spy.puts[len(first_attempt_puts) :]
    assert first_attempt_puts
    assert set(first_attempt_puts) <= set(second_attempt_puts)
    assert set(spy.blobs) == set(second_attempt_puts)
    [output] = _outputs(engine, job.id)
    assert set(second_attempt_puts) == {output.markdown_key, output.manifest_key}


def test_an_upload_outliving_its_claim_writes_no_success(monkeypatch, db_session: Session) -> None:
    """An upload finishing after a stale sweep failed the job and another claim took attempt 2 leaves attempt 2's row alone."""
    engine = db_session.get_bind()
    convert_inline(monkeypatch)
    spy = install_spy_s3(monkeypatch)
    job = make_queued_conversion(db_session, "bm_upload_outlived")
    config = ConversionConfig(_env_file=None, worker_stale_job_minutes=30)

    def _sweep_and_reclaim(key: str) -> None:
        if not key.endswith("manifest.json"):
            return
        other = ConversionStageHandler(config)
        with Session(engine) as session:
            stranded = session.get(ConversionJob, job.id)
            stranded.started_at = utcnow() - dt.timedelta(minutes=60)
            session.add(stranded)
            session.commit()
            assert other.recover_stale(session) == [job.id]
            session.commit()
            assert other.claim_next(session) == job.id
            session.commit()

    spy.before_put = _sweep_and_reclaim
    runner = conversion_runner(engine, config)

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    with Session(engine) as session:
        row = session.get(ConversionJob, job.id)
        assert (row.status, row.attempts, row.phase) == (WorkUnitStatus.RUNNING, 2, "convert")
        kinds = [event.kind for event in events_for_job(session, job.id)]
    assert ConversionEventKind.SUCCEEDED not in kinds
    assert _outputs(engine, job.id) == []
    counters = runner.metrics.counters
    assert "pipeline.work_unit.succeeded" not in counters
    assert runner.metrics.counter_tags["pipeline.work_unit.finalize_skipped"]["outcome"] == "succeeded"


def test_the_attempt_cap_ends_retries_while_the_put_loop_keeps_its_own_count(monkeypatch, db_session: Session) -> None:
    """``worker_max_attempts`` makes the second retryable failure permanent; each attempt still makes ``retry_max_attempts`` PUT tries."""
    engine = db_session.get_bind()
    converted = convert_inline(monkeypatch)
    spy = install_spy_s3(monkeypatch)
    tries: list[str] = []
    spy.before_put = _refuse(tries)
    job = make_queued_conversion(db_session, "bm_upload_capped")
    config = ConversionConfig(_env_file=None, worker_max_attempts=2, retry_max_attempts=3, retry_base_delay_seconds=0)

    with no_thread_leaks(action="raise"):
        conversion_runner(engine, config).run_until_idle()

    assert converted == [job.id, job.id]
    assert len(tries) == 6
    with Session(engine) as session:
        capped = session.get(ConversionJob, job.id)
        assert (capped.status, capped.attempts) == (WorkUnitStatus.FAILED, 2)
        assert capped.earliest_next_attempt_at is None
        assert capped.finished_at is not None


def test_a_job_stranded_in_either_phase_is_recovered_alike_and_converted_again(
    monkeypatch, db_session: Session
) -> None:
    """Stale recovery gives a ``convert`` and an ``upload`` job the same status, wait, and cause; both reconvert."""
    engine = db_session.get_bind()
    converted = convert_inline(monkeypatch)
    install_spy_s3(monkeypatch)
    started_at = utcnow() - dt.timedelta(minutes=60)
    job_ids: list[int] = []
    for phase in ("convert", "upload"):
        job = make_queued_conversion(db_session, f"bm_stranded_{phase}")
        job.status = WorkUnitStatus.RUNNING
        job.phase = phase
        job.attempts = 1
        job.started_at = started_at
        db_session.add(job)
        db_session.commit()
        job_ids.append(job.id)
    runner = conversion_runner(engine, ConversionConfig(_env_file=None, worker_stale_job_minutes=30))

    assert runner.recover_stale() == 2

    with Session(engine) as session:
        rows = [session.get(ConversionJob, job_id) for job_id in job_ids]
        states = {
            (row.status, row.earliest_next_attempt_at, row.error_code, row.finished_at, row.phase) for row in rows
        }
        causes = {
            (event.kind, event.from_status, event.to_status, event.payload_json)
            for job_id in job_ids
            for event in events_for_job(session, job_id)
            if event.kind == ConversionEventKind.RECOVERED_STALE
        }
    assert len(states) == 1
    [(status, wait, _error_code, finished_at, phase)] = states
    assert (status, finished_at, phase) == (WorkUnitStatus.FAILED, None, None)
    assert wait is not None
    assert len(causes) == 1

    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    assert sorted(converted) == sorted(job_ids)
    with Session(engine) as session:
        assert {session.get(ConversionJob, job_id).status for job_id in job_ids} == {WorkUnitStatus.SUCCEEDED}
