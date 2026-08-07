"""Tests for the conversion-coupled enqueue resolvers and backfill target selection.

``enqueue_output`` / ``enqueue_backfill_outputs`` resolve the durable
``source_id`` from the conversion output and delegate to the domain enqueue
(dedupe on ``idempotency_key``). ``latest_output_ids_per_source`` selects the
corpus-scan target set a backfill enqueues. FK enforcement is off, so standalone
``conversion_outputs`` rows suffice alongside the work-unit table.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, create_engine, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.enqueue import enqueue_backfill_outputs, enqueue_output, latest_output_ids_per_source
from aizk.pipeline.invalidation import ReprocessingConfirmationError
from aizk.pipeline.lifecycle import WorkUnitStatus

_UUID_A = UUID("11111111-1111-1111-1111-111111111111")
_UUID_B = UUID("22222222-2222-2222-2222-222222222222")
_UUID_C = UUID("33333333-3333-3333-3333-333333333333")

_EPOCH = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def _make_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'enqueue.db'}")
    ConversionOutput.__table__.create(engine)
    ContextualizationJob.__table__.create(engine)
    return engine


def _add_output(
    session: Session,
    *,
    output_id: int,
    source_id: UUID,
    created_at: dt.datetime | None = None,
) -> None:
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
            created_at=created_at if created_at is not None else _EPOCH,
        )
    )


def test_enqueue_output_resolves_source_id_from_the_conversion_output(tmp_path: Path) -> None:
    """The work-unit carries the source_id resolved from its conversion output."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=42, source_id=_UUID_A)
        session.commit()

        job = enqueue_output(session, 42)
        session.commit()

        assert job.conversion_output_id == 42
        assert job.source_id == _UUID_A
        assert job.status is WorkUnitStatus.QUEUED


def test_enqueue_output_rejects_unknown_output(tmp_path: Path) -> None:
    """Enqueuing a locator with no conversion output is rejected, recording nothing."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="conversion output 7 not found"):
            enqueue_output(session, 7)
        assert session.exec(select(ContextualizationJob)).all() == []


def test_enqueue_backfill_resolves_each_and_dedupes(tmp_path: Path) -> None:
    """Backfill resolves and enqueues each output; re-enqueue reuses the open unit."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        session.commit()

        first = enqueue_backfill_outputs(session, [1, 2], confirmed=True)
        session.commit()
        # A second backfill overlapping the same outputs reuses the open units.
        second = enqueue_backfill_outputs(session, [1, 2], confirmed=True)
        session.commit()

        assert {j.source_id for j in first} == {_UUID_A, _UUID_B}
        assert [j.id for j in second] == [j.id for j in first]
        assert len(session.exec(select(ContextualizationJob)).all()) == 2


def test_enqueue_backfill_outputs_requires_confirmation(tmp_path: Path) -> None:
    """The corpus-wide backfill resolver refuses to enqueue until explicitly confirmed."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        session.commit()

        with pytest.raises(ReprocessingConfirmationError, match="will not run until it is explicitly confirmed"):
            enqueue_backfill_outputs(session, [1])
        assert session.exec(select(ContextualizationJob)).all() == [], "nothing is enqueued without confirmation"


def test_latest_output_ids_per_source_selects_one_output_per_source(tmp_path: Path) -> None:
    """A re-converted source contributes only its newest output, not every historical one."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_A, created_at=_EPOCH + dt.timedelta(days=1))
        _add_output(session, output_id=3, source_id=_UUID_B, created_at=_EPOCH + dt.timedelta(days=2))
        session.commit()

        assert set(latest_output_ids_per_source(session)) == {2, 3}


def test_latest_output_ids_per_source_breaks_created_at_ties_within_a_source(tmp_path: Path) -> None:
    """Two outputs sharing a timestamp resolve to one deterministic winner, not an arbitrary row."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=10, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=11, source_id=_UUID_A, created_at=_EPOCH)
        session.commit()

        assert latest_output_ids_per_source(session) == [11], "the highest id wins a timestamp tie"


def test_latest_output_ids_per_source_orders_by_created_at_then_source_id(tmp_path: Path) -> None:
    """The result is a total order, so a --limit sample is reproducible across runs."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        # _UUID_C and _UUID_A share a timestamp; source_id breaks the tie, so the
        # ordering does not depend on insertion or physical row order.
        _add_output(session, output_id=1, source_id=_UUID_C, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=3, source_id=_UUID_B, created_at=_EPOCH - dt.timedelta(days=1))
        session.commit()

        assert latest_output_ids_per_source(session) == [3, 2, 1]


def test_latest_output_ids_per_source_limit_caps_the_selection(tmp_path: Path) -> None:
    """``limit`` truncates the ordered selection to its first N sources."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_B, created_at=_EPOCH + dt.timedelta(days=1))
        _add_output(session, output_id=3, source_id=_UUID_C, created_at=_EPOCH + dt.timedelta(days=2))
        session.commit()

        assert latest_output_ids_per_source(session, limit=2) == [1, 2]


def test_latest_output_ids_per_source_is_empty_without_conversion_outputs(tmp_path: Path) -> None:
    """An unconverted corpus selects nothing rather than raising."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        assert latest_output_ids_per_source(session) == []


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_latest_output_ids_per_source_rejects_a_limit_that_does_not_bound(tmp_path: Path, limit: int) -> None:
    """A non-positive limit is refused rather than passed to SQLite, which reads -1 as no limit.

    The CLI rejects these at its boundary, but the domain function is callable
    from notebooks and future surfaces, so it refuses them itself rather than
    silently widening a bounded scan into a corpus-wide one.
    """
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        session.commit()

        with pytest.raises(ValueError, match="limit must be a positive integer"):
            latest_output_ids_per_source(session, limit=limit)
