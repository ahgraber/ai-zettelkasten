"""Worker-driven tests for the contextualization stage adapter.

Drives :class:`aizk.graph.handler.ContextualizationStageHandler` through the
shared :class:`aizk.pipeline.runner.StageRunner` over a real (file-based) SQLite
database, exercising the full claim → execute (own transaction) → finalize path
with a deterministic stub model and Markdown source:

- the happy path runs a queued unit to ``SUCCEEDED``, emits transition events
  carrying ``run_id`` + ``source_id``, and persists the chunking run + chunks and
  the summary + variant records;
- re-execution after stale recovery creates no duplicate runs or rows and again
  reaches ``SUCCEEDED`` (the at-least-once / own-transaction path).

The engine has no implicit-BEGIN listener: the runner and the handler each emit
``BEGIN IMMEDIATE`` explicitly, matching production's serialized writer.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
from pathlib import Path
from uuid import UUID

from pyleak import no_thread_leaks
import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.graph.content_index import CONTENT_FTS_DDL
from aizk.graph.datamodel import (
    Chunk,
    ContextualizationJob,
    ContextualizedChunk,
    DocumentSummary,
)
from aizk.graph.handler import DEFAULT_RETRY_MAX_ATTEMPTS, ContextualizationStageHandler
from aizk.graph.llm import StubLLMClient
from aizk.graph.workunit import Cancelled, LoadedMarkdown, enqueue_document
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.pipeline.run import PipelineRun, RunStatus
from aizk.pipeline.runner import StageRunner
from aizk.utilities.hashing import compute_markdown_hash

_AIZK_UUID = UUID("11111111-1111-1111-1111-111111111111")
_OUTPUT_ID = 42

_MARKDOWN = """# Title

A paragraph under the title with enough text to be a real chunk.

## Section One

Content for the first section that the splitter carves into a chunk.

## Section Two

Content for the second section, distinct from the first.
"""


@dataclass
class _StubMarkdownSource:
    """A deterministic Markdown source returning fixed text with its true hash."""

    text: str

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        """Return the stub Markdown and its computed hash for any locator."""
        return LoadedMarkdown(text=self.text, markdown_hash_xx64=compute_markdown_hash(self.text))


class _AlwaysCurrent:
    """A freshness stub treating every output as the source's latest."""

    def is_current(self, session: Session, source_id: UUID, conversion_output_id: int) -> bool:
        return True


def _make_engine(tmp_path: Path):
    """Build a file-based SQLite engine with the graph + pipeline schema (no BEGIN listener)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'stage.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(CONTENT_FTS_DDL))
    return engine


def _make_runner(engine, client: StubLLMClient) -> StageRunner:
    """Build a StageRunner over the contextualization handler with test-fast timing."""
    handler = ContextualizationStageHandler(engine, client, _StubMarkdownSource(_MARKDOWN), _AlwaysCurrent())
    return StageRunner(
        handler,
        engine,
        poll_interval=0.01,
        stale_recovery_interval=1000.0,
        cancel_grace=1.0,
        force_exit=lambda _code: None,
    )


def _make_handler(engine) -> ContextualizationStageHandler:
    """Build a handler over the test engine with deterministic dependencies."""
    return ContextualizationStageHandler(engine, StubLLMClient(), _StubMarkdownSource(_MARKDOWN), _AlwaysCurrent())


def _seed_job(engine, *, status: WorkUnitStatus, attempts: int = 1) -> int:
    """Insert one work-unit in the given status; return its id."""
    with Session(engine) as session:
        job = ContextualizationJob(
            idempotency_key=f"conversion_output:{_OUTPUT_ID}",
            conversion_output_id=_OUTPUT_ID,
            source_id=_AIZK_UUID,
            status=status,
            attempts=attempts,
        )
        session.add(job)
        session.commit()
        return job.id


def _active_runs_by_stage(session: Session) -> dict[str, PipelineRun]:
    runs = session.exec(select(PipelineRun).where(PipelineRun.status == RunStatus.ACTIVE)).all()
    return {r.stage: r for r in runs}


def test_queued_unit_runs_to_success_with_events_and_records(tmp_path: Path) -> None:
    """A queued unit runs through the runner to SUCCEEDED, emitting events and persisting records."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        job = enqueue_document(session, conversion_output_id=_OUTPUT_ID, source_id=_AIZK_UUID, queue_max_depth=0)
        session.commit()
        job_id = job.id

    runner = _make_runner(engine, StubLLMClient())
    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        assert job is not None
        assert job.status is WorkUnitStatus.SUCCEEDED
        assert job.attempts == 1
        assert job.finished_at is not None

        # The three source-scoped runs and their rows are persisted.
        active = _active_runs_by_stage(session)
        assert set(active) == {"chunking", "document_summary", "chunk_contextualization"}
        assert all(r.scope_id == str(_AIZK_UUID) for r in active.values())
        chunks = session.exec(select(Chunk)).all()
        variants = session.exec(select(ContextualizedChunk)).all()
        assert len(chunks) >= 1
        assert len(session.exec(select(DocumentSummary)).all()) == 1
        assert len(variants) == len(chunks)

        # The work-unit's transition events carry source_id throughout, and the
        # terminal success event carries the chunking run id.
        events = session.exec(
            select(PipelineEvent)
            .where(PipelineEvent.stage == "contextualization", PipelineEvent.work_unit_ref == str(job_id))
            .order_by(PipelineEvent.event_id)
        ).all()
        kinds = [e.kind for e in events]
        assert kinds == ["claimed", "succeeded"]
        assert all(e.source_id == _AIZK_UUID for e in events)
        succeeded = next(e for e in events if e.kind == "succeeded")
        assert succeeded.run_id == active["chunking"].id


def test_reexecution_is_idempotent(tmp_path: Path) -> None:
    """A unit re-executed after stale recovery creates no duplicate runs/rows and re-succeeds."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        job = enqueue_document(session, conversion_output_id=_OUTPUT_ID, source_id=_AIZK_UUID, queue_max_depth=0)
        session.commit()
        job_id = job.id

    runner = _make_runner(engine, StubLLMClient())
    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    # Snapshot the produced runs and row counts after the first successful run.
    with Session(engine) as session:
        first_runs = {r.stage: r.id for r in session.exec(select(PipelineRun)).all()}
        first_chunks = len(session.exec(select(Chunk)).all())
        first_summaries = len(session.exec(select(DocumentSummary)).all())
        first_variants = len(session.exec(select(ContextualizedChunk)).all())

        # Strand the unit in RUNNING with an old start time, simulating a process
        # that committed its domain writes but died before finalize.
        job = session.get(ContextualizationJob, job_id)
        job.status = WorkUnitStatus.RUNNING
        job.started_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        session.add(job)
        session.commit()

    # Recovery resets the stranded unit to eligible; the next run re-executes it.
    # The runner's recover_stale() returns the count of reclaimed units.
    recovered = runner.recover_stale()
    assert recovered == 1
    with no_thread_leaks(action="raise"):
        runner.run_until_idle()

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        assert job.status is WorkUnitStatus.SUCCEEDED

        # No duplicate runs or rows: the idempotent write path reused every active run.
        second_runs = {r.stage: r.id for r in session.exec(select(PipelineRun)).all()}
        assert second_runs == first_runs
        assert len(session.exec(select(Chunk)).all()) == first_chunks
        assert len(session.exec(select(DocumentSummary)).all()) == first_summaries
        assert len(session.exec(select(ContextualizedChunk)).all()) == first_variants
        # Exactly one active run per stage remains (no re-supersession churn).
        assert set(_active_runs_by_stage(session)) == {"chunking", "document_summary", "chunk_contextualization"}


# --------------------------------------------------------------------------- #
# Cancellation honoring (an operator cancel of a RUNNING unit is never lost)
# --------------------------------------------------------------------------- #


def test_execute_skips_an_already_cancelled_unit(tmp_path: Path) -> None:
    """A unit cancelled before execution does no work (no runs written)."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.CANCELLED)

    result = _make_handler(engine).execute(job_id)

    assert result is None
    with Session(engine) as session:
        assert session.exec(select(PipelineRun)).all() == []


def test_finalize_does_not_overwrite_a_concurrent_cancel(tmp_path: Path) -> None:
    """A cancel that lands while the unit runs wins: finalize won't write SUCCEEDED over it."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    # An operator cancel commits CANCELLED while the worker is between execute and finalize.
    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        job.status = WorkUnitStatus.CANCELLED
        session.add(job)
        session.commit()

    # The runner then finalizes the (now-stale) successful outcome.
    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.SUCCEEDED))
        session.commit()

    with Session(engine) as session:
        assert session.get(ContextualizationJob, job_id).status is WorkUnitStatus.CANCELLED


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
    """The cancel probe reports a DB-committed cancel without any in-process request.

    An operator cancel is committed by the API (a different process); the in-process
    request set never sees it, so the probe must also consult the durable status.
    """
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    assert handler._is_cancelled(job_id) is False  # running, nothing requested

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        job.status = WorkUnitStatus.CANCELLED
        session.add(job)
        session.commit()

    assert handler._is_cancelled(job_id) is True  # durable cancel observed, no in-process request


def test_execute_honors_a_durable_cancel_committed_mid_run(tmp_path: Path) -> None:
    """An operator cancel committed to the DB while the unit runs stops it before any run is written.

    The unit is RUNNING (not cancelled) at execute entry, so the entry guard passes;
    the operator API then commits CANCELLED during the model passes. The unit-of-work's
    pre-persist probe consults the durable status and skips the domain write, so no
    chunking/summary/variant runs are committed for the cancelled unit.
    """
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)

    class _CancellingClient(StubLLMClient):
        """Commits an operator CANCELLED on its first model call, simulating the API."""

        def generate(self, prompt: str) -> str:
            with Session(engine) as session:
                job = session.get(ContextualizationJob, job_id)
                if job.status is WorkUnitStatus.RUNNING:
                    job.status = WorkUnitStatus.CANCELLED
                    session.add(job)
                    session.commit()
            return super().generate(prompt)

    handler = ContextualizationStageHandler(
        engine, _CancellingClient(), _StubMarkdownSource(_MARKDOWN), _AlwaysCurrent()
    )
    result = handler.execute(job_id)

    assert isinstance(result, Cancelled)
    with Session(engine) as session:
        assert session.exec(select(PipelineRun)).all() == []


def test_finalize_bridge_survives_a_rolled_back_retry(tmp_path: Path) -> None:
    """A finalize that fails (rolls back) does not strip the execute->finalize bridge.

    The runner retains the slot and retries finalize on a finalize-time DB error;
    the per-handle success bridge must persist across that retry (cleanup releases it
    only after the durable commit), so the retry still records the chunking run id as
    the backward-trace root rather than ``run_id=None``.
    """
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    handler.execute(job_id)  # stashes (chunking_run_id, variant_count) in the success bridge
    with Session(engine) as session:
        chunking_run_id = session.exec(
            select(PipelineRun.id).where(PipelineRun.stage == "chunking", PipelineRun.status == RunStatus.ACTIVE)
        ).one()

    # First finalize fails durably (simulate the runner's finalize DB error): roll back.
    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.SUCCEEDED))
        session.rollback()

    # The runner retries finalize on the next reap (slot retained, no cleanup yet).
    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.SUCCEEDED))
        session.commit()

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        assert job.status is WorkUnitStatus.SUCCEEDED
        succeeded = session.exec(
            select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id), PipelineEvent.kind == "succeeded")
        ).one()
        assert succeeded.run_id == chunking_run_id  # the bridge survived the rolled-back retry


def test_failed_unit_persists_a_scrubbed_error_message(tmp_path: Path) -> None:
    """A failing unit records a stable code + scrubbed category message, not raw exception text.

    Raw exception text (provider/S3/DB) may embed object keys, endpoints, or SQL; it
    must not land in ``error_message``, which the operator API persists and returns.
    """
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    leaked_detail = "s3://private-bucket/object-key-9f3a"

    class _LeakySource:
        def load(self, conversion_output_id: int) -> LoadedMarkdown:
            raise RuntimeError(f"connection to {leaked_detail} failed: token=abcd1234")

    handler = ContextualizationStageHandler(engine, StubLLMClient(), _LeakySource(), _AlwaysCurrent())

    with pytest.raises(RuntimeError):
        handler.execute(job_id)
    outcome = handler.map_result(RuntimeError("boom"))  # retryable FAILED

    with Session(engine) as session:
        handler.finalize(session, job_id, outcome)
        session.commit()

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        assert job.status is WorkUnitStatus.FAILED
        assert job.error_code == "RuntimeError"  # stable classifier retained
        assert leaked_detail not in (job.error_message or "")
        assert "token=" not in (job.error_message or "")
        assert job.error_message == "a transient processing error occurred"


# --------------------------------------------------------------------------- #
# Terminal-outcome mapping and finalize transitions (the work-unit lifecycle:
# retry classification + bound, timeout, and runner-cancel all reach a terminal
# status with its co-committed event)
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


def test_finalize_retryable_failure_below_cap_reschedules(tmp_path: Path) -> None:
    """A retryable FAILED below the attempt cap stays re-eligible with a backoff, not finished."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING, attempts=1)  # cap is DEFAULT_RETRY_MAX_ATTEMPTS (3)
    handler = _make_handler(engine)
    handler._errors[job_id] = ("contextualization_error", "transient model error")

    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE))
        session.commit()

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        assert job.status is WorkUnitStatus.FAILED
        assert job.earliest_next_attempt_at is not None, "below the cap the unit is re-eligible after a backoff"
        assert job.finished_at is None, "a retryable failure below the cap is not terminal"
        event = session.exec(
            select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id), PipelineEvent.kind == "failed")
        ).one()
        assert event.to_status == "failed"
        assert json.loads(event.payload_json)["retryable"] is True


def test_finalize_bounded_retry_reaches_terminal_permanent_at_cap(tmp_path: Path) -> None:
    """At the attempt cap a retryable FAILED becomes permanent: finished, with no further retry-wait.

    This is the spec's retry bound — a persistently failing unit reaches a terminal
    failed status rather than retrying without limit.
    """
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING, attempts=DEFAULT_RETRY_MAX_ATTEMPTS)  # at the cap
    handler = _make_handler(engine)
    handler._errors[job_id] = ("contextualization_error", "still failing")

    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE))
        session.commit()

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
        assert job.status is WorkUnitStatus.FAILED
        assert job.finished_at is not None, "at the cap the unit is terminal"
        assert job.earliest_next_attempt_at is None, "at the cap there is no further retry-wait"
        event = session.exec(
            select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id), PipelineEvent.kind == "failed")
        ).one()
        assert json.loads(event.payload_json)["retryable"] is False


def test_finalize_timed_out_records_terminal_timed_out(tmp_path: Path) -> None:
    """A runner-resolved TIMED_OUT outcome finalizes to a terminal timed_out status + event."""
    engine = _make_engine(tmp_path)
    job_id = _seed_job(engine, status=WorkUnitStatus.RUNNING)
    handler = _make_handler(engine)

    with Session(engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.TIMED_OUT))
        session.commit()

    with Session(engine) as session:
        job = session.get(ContextualizationJob, job_id)
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
        job = session.get(ContextualizationJob, job_id)
        assert job.status is WorkUnitStatus.CANCELLED
        assert job.finished_at is not None
        event = session.exec(
            select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id), PipelineEvent.kind == "cancelled")
        ).one()
        assert event.to_status == "cancelled"
        assert json.loads(event.payload_json)["cancellation_reason"] == "runner_cancel"
