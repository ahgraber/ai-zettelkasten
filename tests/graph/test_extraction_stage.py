"""Worker-driven tests for the extraction stage adapter.

Drives :class:`aizk.graph.extraction_handler.ExtractionStageHandler` through the
shared :class:`aizk.pipeline.runner.StageRunner` over a real (file-based) SQLite
database, exercising the full claim → execute (own transaction) → finalize path
with a deterministic stub extractor:

- the happy path runs a queued unit to ``SUCCEEDED``, emits transition events
  carrying ``run_id`` + ``source_id``, and persists the extraction run's
  mentions and co-occurrence links;
- re-execution after stale recovery creates no duplicate runs or rows and again
  reaches ``SUCCEEDED`` (the at-least-once / own-transaction path);
- retryable-vs-permanent failure classification, cancellation honoring, and the
  generic terminal-outcome transitions (timeout, runner cancel, bounded retry).

The engine has no implicit-BEGIN listener: the runner and the handler each emit
``BEGIN IMMEDIATE`` explicitly, matching production's serialized writer.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pyleak import no_thread_leaks
import pytest
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.graph.datamodel import Chunk, ChunkRunManifest, ExtractionJob, Mention, MentionCooccurrence
from aizk.graph.extraction import Detection
from aizk.graph.extraction_handler import DEFAULT_RETRY_MAX_ATTEMPTS, ExtractionStageHandler
from aizk.graph.extraction_workunit import enqueue_extraction
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.pipeline.run import PipelineRun, RunStatus, record_run
from aizk.pipeline.runner import StageRunner

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

_SOURCE_A = UUID("11111111-1111-1111-1111-111111111111")

_SCHEMA_TABLES = [
    Chunk.__table__,
    ChunkRunManifest.__table__,
    Mention.__table__,
    MentionCooccurrence.__table__,
    ExtractionJob.__table__,
    PipelineRun.__table__,
    PipelineEvent.__table__,
]


class _StubExtractor:
    """Deterministic EntityExtractor test double returning configured detections per input text.

    Mirrors ``tests/graph/test_extraction_run_mode.py``'s ``_StubExtractor``: a
    fixed ``text -> detections`` mapping keyed by exact input text, and a
    ``calls`` log so a test can assert the single extractor access point was
    (or was not) reached.
    """

    def __init__(self, detections_by_text: dict[str, list[Detection]], *, extractor_version: str = "stub/v1") -> None:
        """Store the fixed ``text -> detections`` mapping and ``extractor_version``."""
        self._detections_by_text = detections_by_text
        self.extractor_version = extractor_version
        self.calls: list[str] = []

    def extract(self, text: str) -> "Sequence[Detection]":
        """Record ``text`` at :attr:`calls`, then return its configured detections (empty if unconfigured)."""
        self.calls.append(text)
        return list(self._detections_by_text.get(text, []))


class _RaisingExtractor:
    """An EntityExtractor test double that always raises, simulating a transient model error."""

    extractor_version = "raising/v1"

    def extract(self, text: str) -> "Sequence[Detection]":
        """Always raise a transient error."""
        raise RuntimeError("model backend unavailable")


_CHUNK_TEXT = "Acme Corp met Globex Inc today."
_CHUNK_TEXT_2 = "The company announced strong results."

_DETECTIONS = {
    _CHUNK_TEXT: [
        Detection(surface_form="Acme Corp", span_start=0, span_end=9),
        Detection(surface_form="Globex Inc", span_start=14, span_end=24),
    ],
}


def _make_engine(tmp_path: Path, name: str = "stage.db") -> "Engine":
    """Build a file-based SQLite engine with the extraction + pipeline schema (no BEGIN listener)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine, tables=_SCHEMA_TABLES)
    return engine


def _seed_chunking_run(session: Session, *, source_id: UUID, texts: "Sequence[str]") -> None:
    """Seed an active chunking run and its Chunk rows, bypassing ``persist_chunks``.

    Builds only what ``extract_document`` reads (a source's active chunking run
    and its chunks in document order), mirroring
    ``tests/graph/test_extraction_run_mode.py``'s ``_seed_chunking_run``.
    """
    run = record_run(session, stage=CHUNKING_STAGE, scope_id=str(source_id), derivation_key="chunking-key-1")
    cursor = 0
    for ordinal, text in enumerate(texts):
        chunk = Chunk(
            chunk_id=str(uuid4()),
            content_hash=xxhash.xxh64(text.encode("utf-8")).hexdigest(),
            source_id=str(source_id),
            heading_path_json="[]",
            ordinal=ordinal,
            text=text,
            char_count=len(text),
        )
        session.add(chunk)
        session.add(
            ChunkRunManifest(run_id=run.id, chunk_id=chunk.chunk_id, span_start=cursor, span_end=cursor + len(text))
        )
        cursor += len(text) + 1
    session.commit()


def _make_runner(engine: "Engine", extractor) -> StageRunner:
    """Build a StageRunner over the extraction handler with test-fast timing."""
    handler = ExtractionStageHandler(engine, extractor, input_policy="raw", concurrency=1)
    return StageRunner(
        handler,
        engine,
        poll_interval=0.01,
        stale_recovery_interval=1000.0,
        cancel_grace=1.0,
        force_exit=lambda _code: None,
    )


def _make_handler(engine: "Engine", extractor=None) -> ExtractionStageHandler:
    """Build a handler over the test engine with a deterministic stub extractor."""
    return ExtractionStageHandler(engine, extractor or _StubExtractor(_DETECTIONS), input_policy="raw")


def _seed_job(engine: "Engine", *, status: WorkUnitStatus, attempts: int = 1, source_id: UUID = _SOURCE_A) -> int:
    """Insert one work-unit in the given status; return its id."""
    with Session(engine) as session:
        job = ExtractionJob(
            idempotency_key=f"source:{source_id}",
            source_id=source_id,
            status=status,
            attempts=attempts,
        )
        session.add(job)
        session.commit()
        return job.id


# --------------------------------------------------------------------------- #
# Happy path — queued unit through the runner
# --------------------------------------------------------------------------- #


def test_queued_unit_runs_to_success_with_events_and_records(tmp_path: Path) -> None:
    """A queued unit runs through the runner to SUCCEEDED, emitting events and persisting records."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, texts=[_CHUNK_TEXT])
        job = enqueue_extraction(session, source_id=_SOURCE_A)
        session.commit()
        job_id = job.id

    stub = _StubExtractor(_DETECTIONS)
    runner = _make_runner(engine, stub)
    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job is not None
        assert job.status is WorkUnitStatus.SUCCEEDED
        assert job.attempts == 1
        assert job.finished_at is not None

        run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == "mention_extraction", PipelineRun.status == RunStatus.ACTIVE
            )
        ).one()
        assert run.scope_id == str(_SOURCE_A)

        mentions = session.exec(select(Mention).where(Mention.run_id == run.id)).all()
        assert len(mentions) == 2
        links = session.exec(select(MentionCooccurrence).where(MentionCooccurrence.run_id == run.id)).all()
        assert len(links) == 1

        events = session.exec(
            select(PipelineEvent)
            .where(PipelineEvent.stage == "mention_extraction", PipelineEvent.work_unit_ref == str(job_id))
            .order_by(PipelineEvent.event_id)
        ).all()
        kinds = [e.kind for e in events]
        assert kinds == ["claimed", "succeeded"]
        assert all(e.source_id == _SOURCE_A for e in events)
        succeeded = next(e for e in events if e.kind == "succeeded")
        assert succeeded.run_id == run.id
        # The event payload echoes the persisted mention count.
        assert json.loads(succeeded.payload_json)["mention_count"] == 2

    assert stub.calls == [_CHUNK_TEXT]


def test_reexecution_is_idempotent(tmp_path: Path) -> None:
    """A unit re-executed after stale recovery creates no duplicate runs/rows and re-succeeds."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, texts=[_CHUNK_TEXT])
        job = enqueue_extraction(session, source_id=_SOURCE_A)
        session.commit()
        job_id = job.id

    stub = _StubExtractor(_DETECTIONS)
    runner = _make_runner(engine, stub)
    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    with Session(engine) as session:
        first_run_id = session.exec(
            select(PipelineRun.id).where(
                PipelineRun.stage == "mention_extraction", PipelineRun.status == RunStatus.ACTIVE
            )
        ).one()
        first_mention_count = len(session.exec(select(Mention)).all())

        # Strand the unit in RUNNING with an old start time, simulating a process
        # that committed its domain writes but died before finalize.
        job = session.get(ExtractionJob, job_id)
        job.status = WorkUnitStatus.RUNNING
        job.started_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        session.add(job)
        session.commit()

    recovered = runner.recover_stale()
    assert recovered == 1
    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.SUCCEEDED

        second_run_id = session.exec(
            select(PipelineRun.id).where(
                PipelineRun.stage == "mention_extraction", PipelineRun.status == RunStatus.ACTIVE
            )
        ).one()
        assert second_run_id == first_run_id
        assert len(session.exec(select(Mention)).all()) == first_mention_count


# --------------------------------------------------------------------------- #
# Retryable-vs-permanent classification
# --------------------------------------------------------------------------- #


def test_map_result_classifies_outcomes(tmp_path: Path) -> None:
    """A success maps to SUCCEEDED, a ValueError to permanent FAILED, any other error to retryable FAILED."""
    handler = _make_handler(_make_engine(tmp_path))

    assert handler.map_result(None).status is WorkUnitStatus.SUCCEEDED

    permanent = handler.map_result(ValueError("unprocessable input"))
    assert permanent.status is WorkUnitStatus.FAILED
    assert permanent.retry_class is RetryClass.PERMANENT

    retryable = handler.map_result(RuntimeError("transient model error"))
    assert retryable.status is WorkUnitStatus.FAILED
    assert retryable.retry_class is RetryClass.RETRYABLE


def test_unchunked_source_is_a_permanent_failure(tmp_path: Path) -> None:
    """A source with no active chunking run is unprocessable input: execute raises, mapping to permanent."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    with pytest.raises(ValueError, match="no active chunking run"):
        handler.execute(job_id)
    outcome = handler.map_result(ValueError("no active chunking run"))
    assert outcome.retry_class is RetryClass.PERMANENT


def test_extractor_error_is_a_retryable_failure(tmp_path: Path) -> None:
    """A raising extractor's error propagates and classifies as a retryable failure."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, texts=[_CHUNK_TEXT])
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine, extractor=_RaisingExtractor())

    with pytest.raises(RuntimeError):
        handler.execute(job_id)
    outcome = handler.map_result(RuntimeError("model backend unavailable"))
    assert outcome.retry_class is RetryClass.RETRYABLE


def test_failed_unit_persists_a_scrubbed_error_message(tmp_path: Path) -> None:
    """A failing unit records a stable code + scrubbed category message, not raw exception text."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, texts=[_CHUNK_TEXT])
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    leaked_detail = "internal-model-endpoint-9f3a"

    class _LeakyExtractor:
        extractor_version = "leaky/v1"

        def extract(self, text: str):
            raise RuntimeError(f"connection to {leaked_detail} failed: token=abcd1234")

    handler = _make_handler(engine, extractor=_LeakyExtractor())

    with pytest.raises(RuntimeError):
        handler.execute(job_id)
    outcome = handler.map_result(RuntimeError("boom"))

    with Session(engine) as session:
        handler.finalize(session, job_id, outcome)
        session.commit()

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.FAILED
        assert job.error_code == "RuntimeError"
        assert leaked_detail not in (job.error_message or "")
        assert job.error_message == "a transient processing error occurred"


def test_finalize_retryable_failure_below_cap_reschedules(tmp_path: Path) -> None:
    """A retryable FAILED below the attempt cap stays re-eligible with a backoff, not finished."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING, attempts=1)
    handler = _make_handler(engine)
    handler._errors[job_id] = ("extraction_error", "transient model error")

    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE))
        session.commit()

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.FAILED
        assert job.earliest_next_attempt_at is not None
        assert job.finished_at is None


def test_finalize_bounded_retry_reaches_terminal_permanent_at_cap(tmp_path: Path) -> None:
    """At the attempt cap a retryable FAILED becomes permanent: finished, with no further retry-wait."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING, attempts=DEFAULT_RETRY_MAX_ATTEMPTS)
    handler = _make_handler(engine)
    handler._errors[job_id] = ("extraction_error", "still failing")

    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE))
        session.commit()

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.FAILED
        assert job.finished_at is not None
        assert job.earliest_next_attempt_at is None


# --------------------------------------------------------------------------- #
# Cancellation honoring
# --------------------------------------------------------------------------- #


def test_execute_skips_an_already_cancelled_unit(tmp_path: Path) -> None:
    """A unit cancelled before execution does no work (no active extraction run written)."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.CANCELLED)

    result = _make_handler(engine).execute(job_id)

    assert result is None
    with Session(engine) as session:
        assert session.exec(select(PipelineRun)).all() == []


def test_execute_skips_a_cancel_requested_unit(tmp_path: Path) -> None:
    """A runner cancel recorded before execution makes the handler write no runs."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    handler.cancel(job_id)
    result = handler.execute(job_id)

    assert result is None
    with Session(engine) as session:
        assert session.exec(select(PipelineRun)).all() == []


def test_is_cancelled_observes_a_durable_db_cancel(tmp_path: Path) -> None:
    """The cancel probe reports a DB-committed cancel without any in-process request."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    assert handler._is_cancelled(job_id) is False

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        job.status = WorkUnitStatus.CANCELLED
        session.add(job)
        session.commit()

    assert handler._is_cancelled(job_id) is True


def test_finalize_does_not_overwrite_a_concurrent_cancel(tmp_path: Path) -> None:
    """A cancel that lands while the unit runs wins: finalize won't write SUCCEEDED over it."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        job.status = WorkUnitStatus.CANCELLED
        session.add(job)
        session.commit()

    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.SUCCEEDED))
        session.commit()

    with Session(engine) as session:
        assert session.get(ExtractionJob, job_id).status is WorkUnitStatus.CANCELLED


def test_finalize_timed_out_records_terminal_timed_out(tmp_path: Path) -> None:
    """A runner-resolved TIMED_OUT outcome finalizes to a terminal timed_out status + event."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.TIMED_OUT))
        session.commit()

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.TIMED_OUT
        assert job.finished_at is not None
        event = session.exec(
            select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id), PipelineEvent.kind == "timed_out")
        ).one()
        assert event.to_status == "timed_out"


def test_finalize_runner_cancel_records_terminal_cancelled(tmp_path: Path) -> None:
    """A runner-driven CANCELLED outcome finalizes to a terminal cancelled status + event."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.CANCELLED))
        session.commit()

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.CANCELLED
        assert job.finished_at is not None
        event = session.exec(
            select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id), PipelineEvent.kind == "cancelled")
        ).one()
        assert event.to_status == "cancelled"
