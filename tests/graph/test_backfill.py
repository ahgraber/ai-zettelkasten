"""Tests for the corpus backfill runs (``aizk.graph.backfill``).

Each run resolves a target set, enqueues it through the stage's existing enqueue
primitives, and reports how much of the target set was newly enqueued versus
reused. Re-running over an unchanged corpus must enqueue nothing new — the
idempotency claim these commands rest on — and ``dry_run`` must leave the
work-unit tables untouched.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.backfill import run_contextualization_backfill, run_extraction_backfill
from aizk.graph.capacity import StageAtCapacityError
from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.invalidation import ReprocessingConfirmationError
from aizk.pipeline.run import PipelineRun, record_run

_UUID_A = UUID("11111111-1111-1111-1111-111111111111")
_UUID_B = UUID("22222222-2222-2222-2222-222222222222")

_EPOCH = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def _make_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    SQLModel.metadata.create_all(
        engine,
        tables=[
            ConversionOutput.__table__,
            ContextualizationJob.__table__,
            ExtractionJob.__table__,
            PipelineRun.__table__,
        ],
    )
    return engine


def _add_output(session: Session, *, output_id: int, source_id: UUID, created_at: dt.datetime = _EPOCH) -> None:
    session.add(
        ConversionOutput(
            id=output_id,
            job_id=output_id,
            source_id=source_id,
            owner_id="owner",
            title="Doc",
            payload_version=1,
            s3_prefix=f"prefix-{output_id}",
            markdown_key=f"prefix-{output_id}/output.md",
            manifest_key=f"prefix-{output_id}/manifest.json",
            markdown_hash_xx64="0011223344556677",
            docling_version="1.0",
            pipeline_name="docling",
            created_at=created_at,
        )
    )


def _count(engine, model) -> int:
    with Session(engine) as session:
        return len(session.exec(select(model)).all())


# --- contextualization ------------------------------------------------------


def test_contextualization_backfill_enqueues_the_latest_output_per_source(tmp_path: Path) -> None:
    """A corpus scan enqueues one unit per source, keyed to that source's newest output."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_A, created_at=_EPOCH + dt.timedelta(days=1))
        _add_output(session, output_id=3, source_id=_UUID_B, created_at=_EPOCH)
        session.commit()

    result = run_contextualization_backfill(engine, output_ids=None, limit=None, confirmed=True, dry_run=False)

    assert (result.targeted, result.enqueued, result.reused) == (2, 2, 0)
    with Session(engine) as session:
        enqueued_outputs = {job.conversion_output_id for job in session.exec(select(ContextualizationJob)).all()}
    assert enqueued_outputs == {2, 3}, "the superseded output 1 is not enqueued"


def test_contextualization_backfill_rerun_enqueues_nothing_new(tmp_path: Path) -> None:
    """Re-running over an unchanged corpus reuses every unit — the idempotency claim."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        session.commit()

    run_contextualization_backfill(engine, output_ids=None, limit=None, confirmed=True, dry_run=False)
    second = run_contextualization_backfill(engine, output_ids=None, limit=None, confirmed=True, dry_run=False)

    assert (second.targeted, second.enqueued, second.reused) == (2, 0, 2)
    assert _count(engine, ContextualizationJob) == 2


def test_contextualization_backfill_dry_run_writes_nothing(tmp_path: Path) -> None:
    """A dry run reports the target set it would enqueue and leaves the table empty."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        session.commit()

    result = run_contextualization_backfill(engine, output_ids=None, limit=None, confirmed=True, dry_run=True)

    assert (result.targeted, result.enqueued, result.reused) == (2, 2, 0)
    assert _count(engine, ContextualizationJob) == 0, "a dry run must not persist work-units"


def test_contextualization_backfill_dry_run_needs_no_confirmation(tmp_path: Path) -> None:
    """Previewing a corpus scan writes nothing, so there is no blast radius to sign off on."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        session.commit()

    result = run_contextualization_backfill(engine, output_ids=None, limit=None, confirmed=False, dry_run=True)

    assert result.targeted == 1
    assert _count(engine, ContextualizationJob) == 0


def test_contextualization_backfill_corpus_scan_requires_confirmation(tmp_path: Path) -> None:
    """An implicit corpus scan without confirmation is refused, enqueueing nothing."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        session.commit()

    with pytest.raises(ReprocessingConfirmationError):
        run_contextualization_backfill(engine, output_ids=None, limit=None, confirmed=False, dry_run=False)
    assert _count(engine, ContextualizationJob) == 0


def test_contextualization_backfill_explicit_output_ids_bypass_the_gate(tmp_path: Path) -> None:
    """An operator-named target set is deliberate intent and is never confirmation-gated."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        session.commit()

    result = run_contextualization_backfill(engine, output_ids=[1], limit=None, confirmed=False, dry_run=False)

    assert (result.targeted, result.enqueued) == (1, 1)
    with Session(engine) as session:
        enqueued_outputs = {job.conversion_output_id for job in session.exec(select(ContextualizationJob)).all()}
    assert enqueued_outputs == {1}, "only the named output is enqueued"


def test_contextualization_backfill_limit_caps_a_corpus_scan(tmp_path: Path) -> None:
    """``limit`` bounds an implicit corpus scan to its first N sources."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_B, created_at=_EPOCH + dt.timedelta(days=1))
        session.commit()

    result = run_contextualization_backfill(engine, output_ids=None, limit=1, confirmed=True, dry_run=False)

    assert result.targeted == 1
    assert _count(engine, ContextualizationJob) == 1


def test_contextualization_backfill_corpus_scan_stops_at_capacity(tmp_path: Path) -> None:
    """A corpus scan admits only the stage's headroom and reports what it admitted."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_B, created_at=_EPOCH + dt.timedelta(days=1))
        session.commit()

    result = run_contextualization_backfill(
        engine, output_ids=None, limit=None, confirmed=True, dry_run=False, queue_max_depth=1
    )

    assert result.targeted == 1
    assert _count(engine, ContextualizationJob) == 1


def test_contextualization_backfill_named_outputs_refuse_at_capacity(tmp_path: Path) -> None:
    """A named enumeration that outruns capacity commits nothing rather than partially."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        session.commit()

    with pytest.raises(StageAtCapacityError):
        run_contextualization_backfill(
            engine, output_ids=[1, 2], limit=None, confirmed=False, dry_run=False, queue_max_depth=1
        )

    assert _count(engine, ContextualizationJob) == 0


def test_contextualization_backfill_rejects_an_unknown_output_id(tmp_path: Path) -> None:
    """A named output that does not exist fails loudly rather than silently enqueueing nothing."""
    engine = _make_engine(tmp_path)

    with pytest.raises(ValueError, match="conversion output 99 not found"):
        run_contextualization_backfill(engine, output_ids=[99], limit=None, confirmed=False, dry_run=False)


# --- extraction -------------------------------------------------------------


def test_extraction_backfill_enqueues_sources_with_an_active_chunking_run(tmp_path: Path) -> None:
    """A corpus scan enqueues one unit per source that has something to extract."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_B), derivation_key="dk-b")
        session.commit()

    result = run_extraction_backfill(engine, source_ids=None, confirmed=True, dry_run=False)

    assert (result.targeted, result.enqueued, result.reused) == (2, 2, 0)
    assert _count(engine, ExtractionJob) == 2


def test_extraction_backfill_rerun_enqueues_nothing_new(tmp_path: Path) -> None:
    """Re-running over an unchanged corpus reuses every unit — the idempotency claim."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.commit()

    run_extraction_backfill(engine, source_ids=None, confirmed=True, dry_run=False)
    second = run_extraction_backfill(engine, source_ids=None, confirmed=True, dry_run=False)

    assert (second.targeted, second.enqueued, second.reused) == (1, 0, 1)
    assert _count(engine, ExtractionJob) == 1


def test_extraction_backfill_dry_run_writes_nothing(tmp_path: Path) -> None:
    """A dry run reports the target set it would enqueue and leaves the table empty."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.commit()

    result = run_extraction_backfill(engine, source_ids=None, confirmed=True, dry_run=True)

    assert (result.targeted, result.enqueued) == (1, 1)
    assert _count(engine, ExtractionJob) == 0, "a dry run must not persist work-units"


def test_extraction_backfill_dry_run_needs_no_confirmation(tmp_path: Path) -> None:
    """Previewing a corpus scan writes nothing, so there is no blast radius to sign off on."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.commit()

    result = run_extraction_backfill(engine, source_ids=None, confirmed=False, dry_run=True)

    assert result.targeted == 1
    assert _count(engine, ExtractionJob) == 0


def test_extraction_backfill_corpus_scan_requires_confirmation(tmp_path: Path) -> None:
    """An implicit corpus scan without confirmation is refused, enqueueing nothing."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        session.commit()

    with pytest.raises(ReprocessingConfirmationError):
        run_extraction_backfill(engine, source_ids=None, confirmed=False, dry_run=False)
    assert _count(engine, ExtractionJob) == 0


def test_extraction_backfill_explicit_source_ids_bypass_the_gate(tmp_path: Path) -> None:
    """An operator-named target set is deliberate intent and is never confirmation-gated."""
    engine = _make_engine(tmp_path)

    result = run_extraction_backfill(engine, source_ids=[_UUID_A], confirmed=False, dry_run=False)

    assert (result.targeted, result.enqueued) == (1, 1)
    with Session(engine) as session:
        assert {job.source_id for job in session.exec(select(ExtractionJob)).all()} == {_UUID_A}


def test_extraction_backfill_corpus_scan_stops_at_capacity(tmp_path: Path) -> None:
    """A corpus scan admits only the stage's headroom and reports what it admitted."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_A), derivation_key="dk-a")
        record_run(session, stage=CHUNKING_STAGE, scope_id=str(_UUID_B), derivation_key="dk-b")
        session.commit()

    result = run_extraction_backfill(engine, source_ids=None, confirmed=True, dry_run=False, queue_max_depth=1)

    assert result.targeted == 1
    assert _count(engine, ExtractionJob) == 1


def test_extraction_backfill_named_sources_refuse_at_capacity(tmp_path: Path) -> None:
    """A named enumeration that outruns capacity commits nothing rather than partially."""
    engine = _make_engine(tmp_path)

    with pytest.raises(StageAtCapacityError):
        run_extraction_backfill(
            engine, source_ids=[_UUID_A, _UUID_B], confirmed=False, dry_run=False, queue_max_depth=1
        )

    assert _count(engine, ExtractionJob) == 0
