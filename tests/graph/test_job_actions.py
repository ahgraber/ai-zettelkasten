"""Behavioral tests for the graph stages' operator transitions.

A retry or a re-admission returns an existing work-unit to a stage's actionable
backlog rather than creating one, so none of the three transitions
(:func:`~aizk.graph.job_actions.apply_contextualization_retry`,
:func:`~aizk.graph.job_actions.apply_extraction_retry`,
:func:`~aizk.graph.job_actions.apply_extraction_readmission`) check capacity:
each must succeed even while the stage's backlog sits at its declared limit.
Each transition keeps ``attempts`` as it is, so the numbers on a unit's event
trail never repeat: the next claim increments the count, and a retry of a unit
at the attempt cap grants exactly one more execution. Every ``requeued`` event
names the acting principal.

A cancel of a ``failed`` unit clears its retry wait and keeps its error fields.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import datetime as dt
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.conversion.datamodel.source import Source
from aizk.graph.capacity import StageAtCapacityError, check_capacity
from aizk.graph.datamodel import (
    INPUT_KIND_RAW,
    Chunk,
    ChunkRunInput,
    ChunkRunManifest,
    ContextualizationJob,
    ContextualizationRunInput,
    ContextualizedChunk,
    ExtractionJob,
    ExtractionRunApplicability,
    ExtractionRunInput,
)
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.extraction_handler import (
    DEFAULT_RETRY_MAX_ATTEMPTS as EXTRACTION_RETRY_MAX_ATTEMPTS,
    ExtractionStageHandler,
)
from aizk.graph.extraction_run import extract_document
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.graph.handler import (
    DEFAULT_RETRY_MAX_ATTEMPTS as CONTEXTUALIZATION_RETRY_MAX_ATTEMPTS,
    ContextualizationStageHandler,
)
from aizk.graph.job_actions import (
    apply_contextualization_cancel,
    apply_contextualization_retry,
    apply_extraction_cancel,
    apply_extraction_readmission,
    apply_extraction_retry,
)
from aizk.graph.llm import STUB_MODEL_CONFIG, StubLLMClient
from aizk.graph.mention_store import ResolvedExtractionConfig
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.graph.workunit import LoadedMarkdown
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.identity import content_fingerprint
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.pipeline.run import PipelineRun, record_run
from aizk.pipeline.stage import HandlerBase
from aizk.utilities.hashing import compute_markdown_hash

#: The extraction configuration the re-admission test resolves against.
_RESOLVED_CONFIG = ResolvedExtractionConfig(
    extractor_version="stub/v1", materializer_version="1", input_policy="contextualized"
)

_CHUNK_TEXT = "Ada Lovelace wrote the first algorithm."

_MOMENT = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

#: Tables the re-admission test's own engine needs. Built separately from the
#: ``engine``/``session`` fixtures (``tests/graph/conftest.py``), whose engine
#: auto-issues ``BEGIN IMMEDIATE`` per transaction; ``extract_document`` opens its
#: own ``BEGIN IMMEDIATE`` transaction (:func:`aizk.graph.db.begin_immediate`), and
#: layering the two would double-issue it.
_READMISSION_SCHEMA_TABLES = [
    Chunk.__table__,
    ChunkRunInput.__table__,
    ChunkRunManifest.__table__,
    ContextualizationRunInput.__table__,
    ContextualizedChunk.__table__,
    ExtractionJob.__table__,
    ExtractionRunApplicability.__table__,
    ExtractionRunInput.__table__,
    PipelineRun.__table__,
    PipelineEvent.__table__,
    Source.__table__,
]


def _make_readmission_engine(tmp_path: Path) -> Engine:
    """Create a plain file-based SQLite engine for the extraction write path."""
    engine = create_engine(f"sqlite:///{tmp_path / 'readmission.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=_READMISSION_SCHEMA_TABLES)
    return engine


class _SilentExtractor:
    """A deterministic extractor that detects nothing, so extraction tests need no real NER model."""

    extractor_version = "stub/v1"

    def extract(self, text: str) -> Sequence[object]:
        """Return no detections for any input text."""
        return []


class _StubMarkdownSource:
    """A Markdown source returning fixed text; the claim and finalize paths under test never load it."""

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        """Return the fixed document."""
        return LoadedMarkdown(text="# doc", markdown_hash_xx64=compute_markdown_hash("# doc"))


class _AlwaysCurrent:
    """A freshness stub treating every output as current; never consulted here."""

    def is_current(self, session: Session, source_id: UUID, conversion_output_id: int) -> bool:
        """Report every output as the source's latest."""
        return True


def _contextualization_handler(engine: Engine) -> HandlerBase:
    """Build the contextualization handler under its default retry policy."""
    return ContextualizationStageHandler(
        engine, StubLLMClient(), _StubMarkdownSource(), _AlwaysCurrent(), model_config=STUB_MODEL_CONFIG
    )


def _extraction_handler(engine: Engine) -> HandlerBase:
    """Build the extraction handler under its default retry policy."""
    return ExtractionStageHandler(engine, _SilentExtractor(), input_policy="raw")


@dataclass(frozen=True)
class _Stage:
    """One graph stage's operator surface: its table, its retry and cancel helpers, and a handler factory."""

    model: type
    unit_fields: Callable[[], dict[str, Any]]
    retry: Callable[..., None]
    cancel: Callable[[Session, Any], None]
    build_handler: Callable[[Engine], HandlerBase]
    retry_max_attempts: int

    def seed(self, session: Session, *, status: WorkUnitStatus, attempts: int) -> Any:
        """Insert one of this stage's work-units in ``status`` with ``attempts``; return the refreshed row."""
        job = self.model(idempotency_key=f"unit:{uuid4()}", status=status, attempts=attempts, **self.unit_fields())
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


_STAGES = [
    pytest.param(
        _Stage(
            model=ContextualizationJob,
            unit_fields=lambda: {"conversion_output_id": 1, "source_id": uuid4()},
            retry=apply_contextualization_retry,
            cancel=apply_contextualization_cancel,
            build_handler=_contextualization_handler,
            retry_max_attempts=CONTEXTUALIZATION_RETRY_MAX_ATTEMPTS,
        ),
        id="contextualization",
    ),
    pytest.param(
        _Stage(
            model=ExtractionJob,
            unit_fields=lambda: {"source_id": uuid4()},
            retry=apply_extraction_retry,
            cancel=apply_extraction_cancel,
            build_handler=_extraction_handler,
            retry_max_attempts=EXTRACTION_RETRY_MAX_ATTEMPTS,
        ),
        id="extraction",
    ),
]


def _seed_chunking_run(engine: Engine, *, source_id: UUID, derivation_key: str) -> None:
    """Activate a chunking run for a source and put one chunk (plus its ``ChunkRunInput``) under it."""
    with Session(engine) as session:
        run = record_run(session, stage=CHUNKING_STAGE, scope_id=source_id, derivation_key=derivation_key)
        session.flush()
        content_hash = xxhash.xxh64(_CHUNK_TEXT.encode("utf-8")).hexdigest()
        chunk = session.exec(
            select(Chunk).where(Chunk.source_id == source_id, Chunk.content_hash == content_hash)
        ).one_or_none()
        if chunk is None:
            chunk = Chunk(
                chunk_id=uuid4(),
                content_hash=content_hash,
                source_id=source_id,
                heading_path_json="[]",
                ordinal=0,
                text=_CHUNK_TEXT,
                char_count=len(_CHUNK_TEXT),
            )
            session.add(chunk)
        session.add(ChunkRunManifest(run_id=run.id, chunk_id=chunk.chunk_id, span_start=0, span_end=len(_CHUNK_TEXT)))
        session.add(
            ChunkRunInput(
                run_id=run.id,
                conversion_output_id=f"co-{run.id}",
                markdown_hash_xx64="deadbeefdeadbeef",
                chunk_set_fingerprint=payload_fingerprint(
                    [PayloadItem(chunk_key(chunk), INPUT_KIND_RAW, content_fingerprint(chunk.text))]
                ),
            )
        )
        session.commit()


def _extract(engine: Engine, source_id: UUID) -> None:
    """Run the extraction write path for one source against the deterministic stub extractor."""
    extract_document(engine, source_id=source_id, extractor=_SilentExtractor(), input_policy="contextualized")


def _events(session: Session, job_id: int) -> list[PipelineEvent]:
    """Return a work-unit's lifecycle events in the order they were recorded."""
    return session.exec(
        select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id)).order_by(PipelineEvent.event_id)
    ).all()


def _event_kinds(session: Session, job_id: int) -> list[str]:
    """Return the recorded lifecycle event kinds for a work-unit, in insertion order."""
    return [event.kind for event in _events(session, job_id)]


def _payload(event: PipelineEvent) -> dict[str, Any]:
    """Return an event's persisted payload."""
    return json.loads(event.payload_json)


# --- retry -------------------------------------------------------------------


def test_contextualization_retry_succeeds_at_capacity(
    session: Session, seed_source, seed_contextualization_job
) -> None:
    """A contextualization retry succeeds while the backlog is at its declared limit, and keeps attempts."""
    limit = 2
    for index in range(limit):
        source = seed_source(session, karakeep_id=f"cap-ctx-{index}")
        seed_contextualization_job(
            session, source_id=source.source_id, conversion_output_id=index, status=WorkUnitStatus.QUEUED
        )
    failed_source = seed_source(session, karakeep_id="cap-ctx-failed")
    job = seed_contextualization_job(
        session, source_id=failed_source.source_id, conversion_output_id=99, status=WorkUnitStatus.FAILED, attempts=3
    )

    with pytest.raises(StageAtCapacityError):
        check_capacity(session, ContextualizationJob, stage=CONTEXTUALIZATION_STAGE, limit=limit)

    apply_contextualization_retry(session, job, principal="operator")
    session.commit()

    session.refresh(job)
    assert job.status is WorkUnitStatus.QUEUED
    assert job.attempts == 3
    assert _event_kinds(session, job.id) == ["requeued"]


def test_extraction_retry_succeeds_at_capacity(session: Session, seed_source, seed_extraction_job) -> None:
    """An extraction retry succeeds while the backlog is at its declared limit, and keeps attempts."""
    limit = 2
    for index in range(limit):
        source = seed_source(session, karakeep_id=f"cap-ext-{index}")
        seed_extraction_job(session, source_id=source.source_id, status=WorkUnitStatus.QUEUED)
    failed_source = seed_source(session, karakeep_id="cap-ext-failed")
    job = seed_extraction_job(session, source_id=failed_source.source_id, status=WorkUnitStatus.FAILED, attempts=3)

    with pytest.raises(StageAtCapacityError):
        check_capacity(session, ExtractionJob, stage=EXTRACTION_STAGE, limit=limit)

    apply_extraction_retry(session, job, principal="operator")
    session.commit()

    session.refresh(job)
    assert job.status is WorkUnitStatus.QUEUED
    assert job.attempts == 3
    assert _event_kinds(session, job.id) == ["requeued"]


@pytest.mark.parametrize("stage", _STAGES)
def test_retry_keeps_attempts_and_names_the_principal(session: Session, stage: _Stage) -> None:
    """A retry leaves ``attempts`` where it was and records the acting principal on the ``requeued`` event."""
    job = stage.seed(session, status=WorkUnitStatus.FAILED, attempts=2)

    stage.retry(session, job, principal="operator-1")
    session.commit()

    session.refresh(job)
    assert (job.status, job.attempts) == (WorkUnitStatus.QUEUED, 2)
    (event,) = _events(session, job.id)
    assert event.attempt == 2
    assert _payload(event) == {"kind": "requeued", "requeue_reason": "operator_retry", "principal": "operator-1"}


@pytest.mark.parametrize("stage", _STAGES)
def test_attempt_numbering_continues_across_a_retry(engine: Engine, session: Session, stage: _Stage) -> None:
    """A unit failed on attempt 2 is claimed on attempt 3 after a retry, and the claim event says 3."""
    job = stage.seed(session, status=WorkUnitStatus.FAILED, attempts=2)
    handler = stage.build_handler(engine)

    stage.retry(session, job, principal="operator")
    session.commit()
    assert handler.claim_next(session) == job.id
    session.commit()

    session.refresh(job)
    assert (job.status, job.attempts) == (WorkUnitStatus.RUNNING, 3)
    claimed = _events(session, job.id)[-1]
    assert (claimed.kind, claimed.attempt) == ("claimed", 3)


@pytest.mark.parametrize("stage", _STAGES)
def test_a_retried_unit_at_the_cap_runs_exactly_once_more(engine: Engine, session: Session, stage: _Stage) -> None:
    """A retry of a permanently failed unit grants one claim; a retryable failure on it is permanent again."""
    job = stage.seed(session, status=WorkUnitStatus.FAILED, attempts=stage.retry_max_attempts)
    handler = stage.build_handler(engine)

    stage.retry(session, job, principal="operator")
    session.commit()
    assert handler.claim_next(session) == job.id
    session.commit()
    handler.finalize(session, job.id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE))
    session.commit()

    session.refresh(job)
    assert job.status is WorkUnitStatus.FAILED
    assert job.attempts == stage.retry_max_attempts + 1
    assert job.earliest_next_attempt_at is None
    assert job.finished_at is not None
    assert _payload(_events(session, job.id)[-1])["retryable"] is False
    assert handler.claim_next(session) is None, "it ran exactly once after the retry"


# --- cancel ------------------------------------------------------------------


@pytest.mark.parametrize("stage", _STAGES)
def test_cancel_of_a_failed_unit_clears_the_wait_and_keeps_the_error_fields(session: Session, stage: _Stage) -> None:
    """Cancelling a unit awaiting its retry drops the wait, keeps the recorded error, and records one event."""
    job = stage.seed(session, status=WorkUnitStatus.FAILED, attempts=1)
    job.earliest_next_attempt_at = _MOMENT
    job.error_code = "stage_error"
    job.error_message = "model backend unavailable"
    job.last_error_at = _MOMENT
    session.add(job)
    session.commit()
    session.refresh(job)
    last_error_at = job.last_error_at

    stage.cancel(session, job)
    session.commit()

    session.refresh(job)
    assert job.status is WorkUnitStatus.CANCELLED
    assert job.earliest_next_attempt_at is None
    assert (job.error_code, job.error_message, job.last_error_at) == (
        "stage_error",
        "model backend unavailable",
        last_error_at,
    )
    assert job.finished_at is not None
    assert _event_kinds(session, job.id) == ["cancelled"]


# --- re-admission --------------------------------------------------------------


def test_extraction_readmission_succeeds_at_capacity_for_a_stale_source(
    tmp_path: Path, seed_source, seed_extraction_job
) -> None:
    """Re-admission of a stale source succeeds at the declared limit, keeps attempts, and names the principal."""
    limit = 2
    engine = _make_readmission_engine(tmp_path)
    with Session(engine) as setup:
        for index in range(limit):
            source = seed_source(setup, karakeep_id=f"cap-readmit-{index}")
            seed_extraction_job(setup, source_id=source.source_id, status=WorkUnitStatus.QUEUED)
        stale_source = seed_source(setup, karakeep_id="cap-readmit-stale")

    stale_source_id = stale_source.source_id
    _seed_chunking_run(engine, source_id=stale_source_id, derivation_key="chunking-v1")
    _extract(engine, stale_source_id)

    with Session(engine) as setup:
        job = seed_extraction_job(setup, source_id=stale_source.source_id, status=WorkUnitStatus.SUCCEEDED, attempts=3)
    job_id = job.id

    # Re-chunking the source after it was extracted is what makes it stale.
    _seed_chunking_run(engine, source_id=stale_source_id, derivation_key="chunking-v2")

    with Session(engine) as session:
        with pytest.raises(StageAtCapacityError):
            check_capacity(session, ExtractionJob, stage=EXTRACTION_STAGE, limit=limit)

        job = session.get(ExtractionJob, job_id)
        apply_extraction_readmission(session, job, resolved=_RESOLVED_CONFIG, principal="operator")
        session.commit()

        session.refresh(job)
        assert job.status is WorkUnitStatus.QUEUED
        assert job.attempts == 3
        (event,) = _events(session, job_id)
        assert _payload(event) == {
            "kind": "requeued",
            "requeue_reason": "operator_readmission",
            "principal": "operator",
        }
