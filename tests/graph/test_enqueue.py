"""Tests for the conversion-coupled enqueue resolvers.

``enqueue_output`` / ``enqueue_backfill_outputs`` resolve the durable
``source_id`` from the conversion output and delegate to the domain enqueue
(dedupe on ``idempotency_key``). FK enforcement is off, so standalone
``conversion_outputs`` rows suffice alongside the work-unit table.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, create_engine, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.enqueue import enqueue_backfill_outputs, enqueue_output
from aizk.pipeline.invalidation import ReprocessingConfirmationError
from aizk.pipeline.lifecycle import WorkUnitStatus

_UUID_A = UUID("11111111-1111-1111-1111-111111111111")
_UUID_B = UUID("22222222-2222-2222-2222-222222222222")


def _make_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'enqueue.db'}")
    ConversionOutput.__table__.create(engine)
    ContextualizationJob.__table__.create(engine)
    return engine


def _add_output(session: Session, *, output_id: int, source_id: UUID) -> None:
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
