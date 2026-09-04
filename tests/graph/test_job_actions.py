"""Behavioral tests for the graph stages' capacity-exempt operator transitions.

A retry or a re-admission returns an existing work-unit to a stage's actionable
backlog rather than creating one, so none of the three transitions
(:func:`~aizk.graph.job_actions.apply_contextualization_retry`,
:func:`~aizk.graph.job_actions.apply_extraction_retry`,
:func:`~aizk.graph.job_actions.apply_extraction_readmission`) check capacity:
each must succeed even while the stage's backlog sits at its declared limit.
Each transition also resets ``attempts`` to zero, granting a fresh retry budget.

``apply_cancel`` is unchanged and is not covered here.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
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
from aizk.graph.extraction_run import extract_document
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.graph.job_actions import (
    apply_contextualization_retry,
    apply_extraction_readmission,
    apply_extraction_retry,
)
from aizk.graph.mention_store import ResolvedExtractionConfig
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.identity import content_fingerprint
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, record_run

#: The extraction configuration the re-admission test resolves against.
_RESOLVED_CONFIG = ResolvedExtractionConfig(
    extractor_version="stub/v1", materializer_version="1", input_policy="contextualized"
)

_CHUNK_TEXT = "Ada Lovelace wrote the first algorithm."

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


def _requeued_kinds(session: Session, job_id: int) -> list[str]:
    """Return the recorded lifecycle event kinds for a work-unit, in insertion order."""
    events = session.exec(select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id))).all()
    return [event.kind for event in events]


# --- retry -------------------------------------------------------------------


def test_contextualization_retry_succeeds_at_capacity(
    session: Session, seed_source, seed_contextualization_job
) -> None:
    """A contextualization retry succeeds while the backlog is at its declared limit, and resets attempts."""
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

    apply_contextualization_retry(session, job)
    session.commit()

    session.refresh(job)
    assert job.status is WorkUnitStatus.QUEUED
    assert job.attempts == 0
    assert _requeued_kinds(session, job.id) == ["requeued"]


def test_extraction_retry_succeeds_at_capacity(session: Session, seed_source, seed_extraction_job) -> None:
    """An extraction retry succeeds while the backlog is at its declared limit, and resets attempts."""
    limit = 2
    for index in range(limit):
        source = seed_source(session, karakeep_id=f"cap-ext-{index}")
        seed_extraction_job(session, source_id=source.source_id, status=WorkUnitStatus.QUEUED)
    failed_source = seed_source(session, karakeep_id="cap-ext-failed")
    job = seed_extraction_job(session, source_id=failed_source.source_id, status=WorkUnitStatus.FAILED, attempts=3)

    with pytest.raises(StageAtCapacityError):
        check_capacity(session, ExtractionJob, stage=EXTRACTION_STAGE, limit=limit)

    apply_extraction_retry(session, job)
    session.commit()

    session.refresh(job)
    assert job.status is WorkUnitStatus.QUEUED
    assert job.attempts == 0
    assert _requeued_kinds(session, job.id) == ["requeued"]


# --- re-admission --------------------------------------------------------------


def test_extraction_readmission_succeeds_at_capacity_for_a_stale_source(
    tmp_path: Path, seed_source, seed_extraction_job
) -> None:
    """Re-admission of a stale source succeeds while the backlog is at its declared limit, and resets attempts."""
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
        apply_extraction_readmission(session, job, resolved=_RESOLVED_CONFIG)
        session.commit()

        session.refresh(job)
        assert job.status is WorkUnitStatus.QUEUED
        assert job.attempts == 0
        assert _requeued_kinds(session, job_id) == ["requeued"]
