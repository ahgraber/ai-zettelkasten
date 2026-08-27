"""Tests for the extraction stage's staleness derivation (``stale_extraction_sources``).

A source is stale when its active extraction run consumed upstream state that has
since been superseded — a re-chunk, or a contextualization run appearing for a
source whose extraction fell back to raw chunk text. Staleness marks work an
operator may re-admit; it never makes a source pending.

Every case drives the real :func:`~aizk.graph.extraction_run.extract_document` over
a deterministic stub extractor, so the run records under test are the ones the write
path produces, not hand-built rows. The conformance case then pins the derivation to
that write path: a stale verdict must predict that re-extracting supersedes, a
current verdict that it reuses.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.graph.contextualization import VARIANT_STAGE
from aizk.graph.datamodel import (
    Chunk,
    ChunkRunManifest,
    ContextualizedChunk,
    ExtractionJob,
    Mention,
    MentionCooccurrence,
)
from aizk.graph.extraction_run import extract_document, is_extraction_stale, stale_extraction_sources
from aizk.graph.extraction_workunit import pending_extraction_sources
from aizk.graph.job_actions import apply_extraction_readmission
from aizk.graph.mention_store import active_extraction_run
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, RunStatus, record_run

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aizk.graph.extraction import Detection

_SCHEMA_TABLES = [
    Chunk.__table__,
    ChunkRunManifest.__table__,
    ContextualizedChunk.__table__,
    ExtractionJob.__table__,
    Mention.__table__,
    MentionCooccurrence.__table__,
    PipelineEvent.__table__,
    PipelineRun.__table__,
]

_NOW = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)

_SOURCE_A = "11111111-1111-1111-1111-111111111111"
_SOURCE_B = "22222222-2222-2222-2222-222222222222"

_CHUNK_TEXT = "Ada Lovelace wrote the first algorithm."


class _SilentExtractor:
    """An :class:`~aizk.graph.extraction.EntityExtractor` that detects nothing.

    Staleness is decided by a run's recorded derivation keys, not by the mentions it
    emits, so these tests need a deterministic extractor, not a productive one.
    """

    extractor_version = "stub/v1"

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return no detections for any input text."""
        return []


def _make_engine(tmp_path: Path, name: str = "staleness.db") -> Engine:
    """Create a file-based SQLite engine carrying only the tables these tests touch."""
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=_SCHEMA_TABLES)
    return engine


def _seed_chunking_run(engine: Engine, *, source_id: str, derivation_key: str) -> None:
    """Activate a chunking run for a source and put one chunk under it.

    Mirrors ``persist_chunks``: the chunk is content-keyed, so a re-chunk over
    unchanged content reuses the existing row and only the manifest generation changes.
    """
    with Session(engine) as session:
        run = record_run(session, stage=CHUNKING_STAGE, scope_id=source_id, derivation_key=derivation_key)
        session.flush()
        content_hash = xxhash.xxh64(_CHUNK_TEXT.encode("utf-8")).hexdigest()
        chunk = session.exec(
            select(Chunk).where(Chunk.source_id == source_id, Chunk.content_hash == content_hash)
        ).one_or_none()
        if chunk is None:
            chunk = Chunk(
                chunk_id=str(uuid4()),
                content_hash=content_hash,
                source_id=source_id,
                heading_path_json="[]",
                ordinal=0,
                text=_CHUNK_TEXT,
                char_count=len(_CHUNK_TEXT),
            )
            session.add(chunk)
        session.add(ChunkRunManifest(run_id=run.id, chunk_id=chunk.chunk_id, span_start=0, span_end=len(_CHUNK_TEXT)))
        session.commit()


def _seed_variant_run(engine: Engine, *, source_id: str, derivation_key: str) -> None:
    """Activate a contextualization run for a source."""
    with Session(engine) as session:
        record_run(session, stage=VARIANT_STAGE, scope_id=source_id, derivation_key=derivation_key)
        session.commit()


def _extract(engine: Engine, source_id: str, *, input_policy: str = "contextualized") -> int:
    """Run the write path for one source and return its active extraction run id."""
    return extract_document(
        engine, source_id=source_id, extractor=_SilentExtractor(), input_policy=input_policy
    ).run_id


def test_a_re_chunked_source_is_stale(tmp_path: Path) -> None:
    """A chunking generation superseded after extraction leaves the extracted run behind."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)

    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        assert stale_extraction_sources(session) == {_SOURCE_A}


def test_a_newly_contextualized_source_is_stale(tmp_path: Path) -> None:
    """An extraction that fell back to raw text is behind once variants exist for the source."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)

    _seed_variant_run(engine, source_id=_SOURCE_A, derivation_key="variants-v1")

    with Session(engine) as session:
        assert stale_extraction_sources(session) == {_SOURCE_A}


def test_a_current_source_is_not_stale(tmp_path: Path) -> None:
    """A source whose extraction consumed its current active upstream state is current."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _seed_variant_run(engine, source_id=_SOURCE_A, derivation_key="variants-v1")
    _extract(engine, _SOURCE_A)

    with Session(engine) as session:
        assert stale_extraction_sources(session) == set()


def test_staleness_is_per_source(tmp_path: Path) -> None:
    """A superseded generation for one source does not mark another source stale."""
    engine = _make_engine(tmp_path)
    for source_id in (_SOURCE_A, _SOURCE_B):
        _seed_chunking_run(engine, source_id=source_id, derivation_key="chunking-v1")
        _extract(engine, source_id)

    _seed_chunking_run(engine, source_id=_SOURCE_B, derivation_key="chunking-v2")

    with Session(engine) as session:
        assert stale_extraction_sources(session) == {_SOURCE_B}


def test_a_source_with_no_extraction_run_is_not_stale(tmp_path: Path) -> None:
    """Staleness describes work already done; an unextracted source has nothing to be behind."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")

    with Session(engine) as session:
        assert stale_extraction_sources(session) == set()


@pytest.mark.parametrize(
    ("supersede", "expected_stale"),
    [
        (True, True),
        (False, False),
    ],
    ids=["re-chunked", "unchanged"],
)
def test_the_staleness_verdict_agrees_with_what_re_extraction_reads(
    tmp_path: Path, supersede: bool, expected_stale: bool
) -> None:
    """A stale verdict predicts that re-extracting supersedes; a current verdict predicts reuse.

    The derivation resolves the current upstream key through the same resolver the
    write path uses, so the two cannot disagree about the same state.
    """
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    first_run_id = _extract(engine, _SOURCE_A)

    if supersede:
        _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        assert (_SOURCE_A in stale_extraction_sources(session)) is expected_stale

    second_run_id = _extract(engine, _SOURCE_A)

    assert (second_run_id != first_run_id) is expected_stale, (
        "re-extraction supersedes exactly when the derivation called the source stale"
    )
    with Session(engine) as session:
        active = active_extraction_run(session, _SOURCE_A)
        assert active is not None
        assert active.id == second_run_id


@pytest.mark.parametrize("supersede", [True, False], ids=["stale", "current"])
def test_the_single_source_check_agrees_with_the_corpus_derivation(tmp_path: Path, supersede: bool) -> None:
    """The per-unit check and the corpus scan return the same verdict for a source.

    The re-extract action asks about one source and the dashboard counts them all.
    If those diverged, a unit could be marked stale and then refused as ineligible.
    """
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    if supersede:
        _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        assert is_extraction_stale(session, _SOURCE_A) is (_SOURCE_A in stale_extraction_sources(session))
        assert is_extraction_stale(session, _SOURCE_A) is supersede


def test_the_single_source_check_is_false_for_an_unextracted_source(tmp_path: Path) -> None:
    """A source with no active extraction run has nothing to be behind."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")

    with Session(engine) as session:
        assert is_extraction_stale(session, _SOURCE_A) is False


def _seed_unit(engine: Engine, source_id: str, status: WorkUnitStatus) -> int:
    """Insert the source's extraction work-unit in a given status; return its id."""
    with Session(engine) as session:
        job = ExtractionJob(idempotency_key=f"source:{source_id}", source_id=UUID(source_id), status=status)
        session.add(job)
        session.commit()
        return job.id


def test_re_admission_requeues_a_stale_finished_unit(tmp_path: Path) -> None:
    """Re-extracting a stale source queues its existing unit again, clearing the prior attempt."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    job_id = _seed_unit(engine, _SOURCE_A, WorkUnitStatus.SUCCEEDED)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        job.attempts = 3
        job.error_code = "boom"
        job.finished_at = _NOW
        apply_extraction_readmission(session, job)
        session.commit()

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.QUEUED
        assert job.attempts == 0
        assert job.error_code is None
        assert job.finished_at is None
        assert job.queued_at is not None
        events = session.exec(select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id))).all()
        assert [event.kind for event in events] == ["requeued"], "the transition is durably recorded"
        assert [event.to_status for event in events] == ["queued"]


@pytest.mark.parametrize("status", [WorkUnitStatus.SUCCEEDED, WorkUnitStatus.FAILED])
def test_re_admission_skips_a_unit_whose_source_is_current(tmp_path: Path, status: WorkUnitStatus) -> None:
    """A finished unit on a current source is ineligible and left exactly as it was."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    job_id = _seed_unit(engine, _SOURCE_A, status)

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        with pytest.raises(ValueError, match="not stale"):
            apply_extraction_readmission(session, job)
        session.rollback()

    with Session(engine) as session:
        assert session.get(ExtractionJob, job_id).status is status


@pytest.mark.parametrize("status", [WorkUnitStatus.QUEUED, WorkUnitStatus.RUNNING])
def test_re_admission_skips_an_unfinished_unit(tmp_path: Path, status: WorkUnitStatus) -> None:
    """A unit that has not finished is already going to run, so re-extraction is ineligible."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    job_id = _seed_unit(engine, _SOURCE_A, status)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        with pytest.raises(ValueError, match="cannot re-extract a work-unit in status"):
            apply_extraction_readmission(session, job)
        session.rollback()

    with Session(engine) as session:
        assert session.get(ExtractionJob, job_id).status is status


def test_a_re_admitted_source_re_extracts_against_current_inputs(tmp_path: Path) -> None:
    """After re-admission the worker's next execution reads current state and supersedes the prior run."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    first_run_id = _extract(engine, _SOURCE_A)
    job_id = _seed_unit(engine, _SOURCE_A, WorkUnitStatus.SUCCEEDED)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        apply_extraction_readmission(session, session.get(ExtractionJob, job_id))
        session.commit()

    # Executing the requeued unit is the stage's normal write path, whatever queued it.
    second_run_id = _extract(engine, _SOURCE_A)

    assert second_run_id != first_run_id
    with Session(engine) as session:
        active = active_extraction_run(session, _SOURCE_A)
        assert active is not None
        assert active.id == second_run_id
        superseded = session.get(PipelineRun, first_run_id)
        assert superseded.status is RunStatus.SUPERSEDED
        assert stale_extraction_sources(session) == set(), "the source is current again"


def test_a_stale_source_is_not_pending(tmp_path: Path) -> None:
    """Staleness never admits work: a stale source stays out of the pending set."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    with Session(engine) as session:
        session.add(
            ExtractionJob(
                idempotency_key=f"source:{_SOURCE_A}",
                source_id=UUID(_SOURCE_A),
                status=WorkUnitStatus.SUCCEEDED,
            )
        )
        session.commit()

    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        assert stale_extraction_sources(session) == {_SOURCE_A}
        assert pending_extraction_sources(session) == []
