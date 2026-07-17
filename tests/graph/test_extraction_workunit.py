"""Tests for the extraction stage's enqueue paths (``aizk.graph.extraction_workunit``).

``enqueue_extraction`` dedupes on ``idempotency_key`` (keyed by the durable
source identity); ``enqueue_extraction_backfill`` scopes itself to sources with
an active chunking run and refuses to run without explicit confirmation,
mirroring ``aizk.graph.workunit``'s enqueue functions and
``tests/graph/test_enqueue.py``'s coverage of the contextualization backfill's
confirmation gate.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_workunit import enqueue_extraction, enqueue_extraction_backfill
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.invalidation import ReprocessingConfirmationError
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, record_run

_UUID_A = UUID("11111111-1111-1111-1111-111111111111")
_UUID_B = UUID("22222222-2222-2222-2222-222222222222")


def _make_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'extraction_enqueue.db'}")
    SQLModel.metadata.create_all(engine, tables=[ExtractionJob.__table__, PipelineRun.__table__])
    return engine


def test_enqueue_extraction_creates_a_queued_unit(tmp_path: Path) -> None:
    """A first enqueue for a source inserts a QUEUED work-unit carrying its source_id."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        job = enqueue_extraction(session, source_id=_UUID_A)
        session.commit()

        assert job.source_id == _UUID_A
        assert job.status is WorkUnitStatus.QUEUED


def test_enqueue_extraction_reuses_the_open_unit(tmp_path: Path) -> None:
    """Re-enqueueing the same source reuses its open work-unit rather than duplicating it (idempotency)."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        first = enqueue_extraction(session, source_id=_UUID_A)
        session.commit()
        second = enqueue_extraction(session, source_id=_UUID_A)
        session.commit()

        assert second.id == first.id
        assert len(session.exec(select(ExtractionJob)).all()) == 1


def test_enqueue_extraction_backfill_resolves_eligible_sources_and_dedupes(tmp_path: Path) -> None:
    """Backfill enqueues one unit per source with an active chunking run; a second pass reuses the open units."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_B), derivation_key="dk-b")
        session.commit()

        first = enqueue_extraction_backfill(session, confirmed=True)
        session.commit()
        second = enqueue_extraction_backfill(session, confirmed=True)
        session.commit()

        assert {j.source_id for j in first} == {_UUID_A, _UUID_B}
        assert [j.id for j in second] == [j.id for j in first]
        assert len(session.exec(select(ExtractionJob)).all()) == 2


def test_enqueue_extraction_backfill_excludes_sources_without_a_chunking_run(tmp_path: Path) -> None:
    """A source with no active chunking run is not enqueued: it has nothing to extract."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.commit()

        jobs = enqueue_extraction_backfill(session, confirmed=True)
        session.commit()

        assert {j.source_id for j in jobs} == {_UUID_A}


def test_enqueue_extraction_backfill_requires_confirmation(tmp_path: Path) -> None:
    """The corpus-wide backfill resolver refuses to enqueue until explicitly confirmed."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.commit()

        with pytest.raises(ReprocessingConfirmationError, match="will not run until it is explicitly confirmed"):
            enqueue_extraction_backfill(session)
        assert session.exec(select(ExtractionJob)).all() == [], "nothing is enqueued without confirmation"
