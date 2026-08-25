"""Tests for the extraction stage's enqueue paths (``aizk.graph.extraction_workunit``).

``enqueue_extraction`` dedupes on ``idempotency_key`` (keyed by the durable source
identity), then honors the stage's declared capacity. ``enqueue_extraction_backfill``
scopes itself to sources with an active chunking run, truncates to the batch's
capacity headroom, and refuses to run without explicit confirmation. These mirror
``aizk.graph.workunit``'s enqueue functions, covered in
``tests/graph/test_enqueue.py``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.graph.capacity import StageAtCapacityError
from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_workunit import (
    enqueue_extraction,
    enqueue_extraction_backfill,
    pending_extraction_sources,
)
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


# --- pending-work derivation ------------------------------------------------


def test_a_chunked_never_extracted_source_is_pending(tmp_path: Path) -> None:
    """A source with an active chunking run and no work-unit is work the stage owes."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.commit()

        assert pending_extraction_sources(session) == [_UUID_A]


@pytest.mark.parametrize("status", list(WorkUnitStatus), ids=lambda status: status.value)
def test_a_source_with_an_extraction_unit_is_not_pending(tmp_path: Path, status: WorkUnitStatus) -> None:
    """The work-unit is keyed by the source alone, so any status covers it — including a terminal one."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.add(ExtractionJob(idempotency_key=f"source:{_UUID_A}", source_id=_UUID_A, status=status))
        session.commit()

        assert pending_extraction_sources(session) == []


def test_a_re_chunked_source_with_a_succeeded_unit_is_not_pending(tmp_path: Path) -> None:
    """Superseded upstream state makes a source stale, not pending: re-extraction stays operator-initiated."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.add(
            ExtractionJob(idempotency_key=f"source:{_UUID_A}", source_id=_UUID_A, status=WorkUnitStatus.SUCCEEDED)
        )
        session.commit()
        # A re-chunk supersedes the first run and leaves a new active one.
        rechunked = record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a-v2")
        session.commit()

        assert rechunked.supersedes_run_id is not None, "the source really was re-chunked"
        assert pending_extraction_sources(session) == []


def test_a_source_without_a_chunking_run_is_not_pending(tmp_path: Path) -> None:
    """A source with nothing chunked has nothing to extract, so it never enters the pending set."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        assert pending_extraction_sources(session) == []


def test_the_extraction_pending_set_is_a_function_of_state_alone(tmp_path: Path) -> None:
    """Two evaluations against identical state yield the identical set — the derivation has no memory."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_B), derivation_key="dk-b")
        session.commit()

        first = pending_extraction_sources(session)
        second = pending_extraction_sources(session)

        assert first == second
        assert set(first) == {_UUID_A, _UUID_B}


def test_unadmitted_extraction_work_stays_pending(tmp_path: Path) -> None:
    """Work a bounded evaluation left out is still pending on the next evaluation."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_B), derivation_key="dk-b")
        session.commit()

        bounded = pending_extraction_sources(session, limit=1)

        assert len(bounded) == 1
        assert set(pending_extraction_sources(session)) == {_UUID_A, _UUID_B}


def test_the_extraction_pending_limit_applies_after_the_anti_join(tmp_path: Path) -> None:
    """A bounded evaluation returns pending work, not a corpus sample that may hold none."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.add(
            ExtractionJob(idempotency_key=f"source:{_UUID_A}", source_id=_UUID_A, status=WorkUnitStatus.QUEUED)
        )
        session.commit()
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_B), derivation_key="dk-b")
        session.commit()

        assert pending_extraction_sources(session, limit=1) == [_UUID_B]


@pytest.mark.parametrize("limit", [0, -1])
def test_the_extraction_pending_query_rejects_a_limit_that_does_not_bound(tmp_path: Path, limit: int) -> None:
    """A non-positive bound would widen the evaluation instead of narrowing it, so it is refused."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session, pytest.raises(ValueError, match="limit must be a positive integer"):
        pending_extraction_sources(session, limit=limit)


def test_enqueue_extraction_refuses_new_work_at_capacity(tmp_path: Path) -> None:
    """At the declared capacity a new work-unit is refused and nothing is added."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        enqueue_extraction(session, source_id=_UUID_A, queue_max_depth=1)
        session.commit()

        with pytest.raises(StageAtCapacityError, match="mention_extraction stage is at capacity"):
            enqueue_extraction(session, source_id=_UUID_B, queue_max_depth=1)
        session.rollback()

        assert len(session.exec(select(ExtractionJob)).all()) == 1


def test_enqueue_extraction_at_capacity_returns_the_existing_unit(tmp_path: Path) -> None:
    """A duplicate bypasses the capacity check: reusing a unit adds no work to the backlog."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        first = enqueue_extraction(session, source_id=_UUID_A, queue_max_depth=1)
        session.commit()

        again = enqueue_extraction(session, source_id=_UUID_A, queue_max_depth=1)
        session.commit()

        assert again.id == first.id
        assert len(session.exec(select(ExtractionJob)).all()) == 1


def test_enqueue_extraction_without_a_declared_limit_accepts_work(tmp_path: Path) -> None:
    """A stage declaring no capacity limit enqueues without a capacity refusal."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        enqueue_extraction(session, source_id=_UUID_A)
        enqueue_extraction(session, source_id=_UUID_B)
        session.commit()

        assert len(session.exec(select(ExtractionJob)).all()) == 2


def test_enqueue_extraction_backfill_admits_only_the_batch_headroom(tmp_path: Path) -> None:
    """A bulk enqueue over more eligible sources than the headroom admits the headroom and leaves the rest."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_B), derivation_key="dk-b")
        session.commit()

        admitted = enqueue_extraction_backfill(session, confirmed=True, queue_max_depth=1)
        session.commit()

        persisted = session.exec(select(ExtractionJob)).all()
        assert len(admitted) == 1
        assert {job.source_id for job in persisted} == {job.source_id for job in admitted}
        assert {job.source_id for job in persisted} < {_UUID_A, _UUID_B}, "the remainder is left unenqueued"


def test_enqueue_extraction_backfill_requires_confirmation(tmp_path: Path) -> None:
    """The corpus-wide backfill resolver refuses to enqueue until explicitly confirmed."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.commit()

        with pytest.raises(ReprocessingConfirmationError, match="will not run until it is explicitly confirmed"):
            enqueue_extraction_backfill(session)
        assert session.exec(select(ExtractionJob)).all() == [], "nothing is enqueued without confirmation"
