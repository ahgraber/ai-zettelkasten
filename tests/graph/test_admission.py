"""Behavioral tests for admission (``aizk.graph.admission``).

Admission creates the work-units a stage's upstream state says should exist. These
tests pin what a pass does — and what it must not do: nothing while the stage is
switched off, nothing for a stage that declared no derivation, nothing twice over
unchanged state, and nothing beyond the stage's capacity.

Both graph stages are exercised through their real adapters against real conversion
outputs and chunking runs, so a pass goes through the same enqueue primitive every
other path uses and the resulting units are directly comparable.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import time
from uuid import UUID

from pyleak import no_thread_leaks
import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.admission import (
    AdmissionLoop,
    admission_adapter_for,
    contextualization_adapter,
    extraction_adapter,
    run_admission_pass,
)
from aizk.graph.config import AdmissionConfig
from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.graph.enqueue import enqueue_output
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.extraction_workunit import enqueue_extraction
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, record_run

_UUID_A = UUID("11111111-1111-1111-1111-111111111111")
_UUID_B = UUID("22222222-2222-2222-2222-222222222222")
_UUID_C = UUID("33333333-3333-3333-3333-333333333333")

_EPOCH = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)

_SCHEMA_TABLES = [
    ConversionOutput.__table__,
    ContextualizationJob.__table__,
    ExtractionJob.__table__,
    PipelineRun.__table__,
]


def _make_engine(tmp_path: Path) -> Engine:
    """Create a file-based SQLite engine carrying only the tables admission touches."""
    engine = create_engine(f"sqlite:///{tmp_path / 'admission.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=_SCHEMA_TABLES)
    return engine


def _add_output(session: Session, *, output_id: int, source_id: UUID, created_at: dt.datetime = _EPOCH) -> None:
    """Insert a conversion output, the upstream artifact contextualization admits over."""
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


def _seed_converted_sources(engine: Engine, count: int) -> None:
    """Give ``count`` sources a conversion output apiece, so each is pending contextualization."""
    with Session(engine) as session:
        for index in range(1, count + 1):
            _add_output(
                session,
                output_id=index,
                source_id=UUID(f"{index:08d}-0000-0000-0000-000000000000"),
                created_at=_EPOCH + dt.timedelta(days=index),
            )
        session.commit()


def _seed_chunked_sources(engine: Engine, *source_ids: UUID) -> None:
    """Give each source an active chunking run, so each is pending extraction."""
    with Session(engine) as session:
        for source_id in source_ids:
            record_run(session, stage=CHUNKING_STAGE, scope_id=str(source_id), derivation_key=f"dk-{source_id}")
        session.commit()


def _units(engine: Engine, model: type) -> list:
    """Return every work-unit row of a stage."""
    with Session(engine) as session:
        return list(session.exec(select(model)).all())


def _config(**overrides: object) -> AdmissionConfig:
    """Build hermetic admission settings, overriding only the fields a test cares about."""
    return AdmissionConfig(_env_file=None, **overrides)


# --- declaration ------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "expected_stage"),
    [(CONTEXTUALIZATION_STAGE, CONTEXTUALIZATION_STAGE), (EXTRACTION_STAGE, EXTRACTION_STAGE)],
)
def test_a_declaring_stage_reports_its_derivation(stage: str, expected_stage: str) -> None:
    """A stage that declares a pending-work derivation reports it when queried."""
    adapter = admission_adapter_for(stage, _config())

    assert adapter is not None
    assert adapter.stage == expected_stage


def test_a_stage_without_a_derivation_reports_none() -> None:
    """A stage that declares no derivation reports none, and admission never creates work for it."""
    assert admission_adapter_for("conversion", _config()) is None


# --- enablement -------------------------------------------------------------


def test_a_disabled_stage_admits_nothing(tmp_path: Path) -> None:
    """Pending work is left alone while automatic admission is switched off for the stage."""
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 2)

    admitted = run_admission_pass(engine, contextualization_adapter(_config()))

    assert admitted == 0
    assert _units(engine, ContextualizationJob) == []


def test_an_enabled_stage_admits_its_pending_work(tmp_path: Path) -> None:
    """With the stage switched on, pending work becomes queued units with no operator action."""
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 2)

    admitted = run_admission_pass(engine, contextualization_adapter(_config(admission_contextualization_enabled=True)))

    units = _units(engine, ContextualizationJob)
    assert admitted == 2
    assert {unit.conversion_output_id for unit in units} == {1, 2}
    assert {unit.status for unit in units} == {WorkUnitStatus.QUEUED}


def test_enabling_one_stage_admits_nothing_for_the_other(tmp_path: Path) -> None:
    """Enablement is per stage: switching contextualization on leaves extraction untouched."""
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 1)
    _seed_chunked_sources(engine, _UUID_A)
    config = _config(admission_contextualization_enabled=True)

    run_admission_pass(engine, contextualization_adapter(config))
    run_admission_pass(engine, extraction_adapter(config))

    assert len(_units(engine, ContextualizationJob)) == 1
    assert _units(engine, ExtractionJob) == []


# --- what a pass admits -----------------------------------------------------


def test_a_pass_admits_exactly_the_pending_set(tmp_path: Path) -> None:
    """Sources that are not pending are untouched; only the pending set gets units."""
    engine = _make_engine(tmp_path)
    _seed_chunked_sources(engine, _UUID_A, _UUID_B)
    with Session(engine) as session:
        # _UUID_A already has a unit, so it is not pending; _UUID_C is in the corpus
        # but unchunked, so it has nothing to extract. Only _UUID_B is pending.
        enqueue_extraction(session, source_id=_UUID_A)
        _add_output(session, output_id=99, source_id=_UUID_C)
        session.commit()

    admitted = run_admission_pass(engine, extraction_adapter(_config(admission_extraction_enabled=True)))

    assert admitted == 1
    assert {unit.source_id for unit in _units(engine, ExtractionJob)} == {_UUID_A, _UUID_B}


def test_a_repeated_pass_over_unchanged_state_admits_nothing_new(tmp_path: Path) -> None:
    """Admitted work is no longer pending, so a second pass creates nothing."""
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 2)
    adapter = contextualization_adapter(_config(admission_contextualization_enabled=True))

    first = run_admission_pass(engine, adapter)
    unit_ids = {unit.id for unit in _units(engine, ContextualizationJob)}
    second = run_admission_pass(engine, adapter)

    assert (first, second) == (2, 0)
    assert {unit.id for unit in _units(engine, ContextualizationJob)} == unit_ids


def test_an_admitted_unit_equals_a_manually_enqueued_one(tmp_path: Path) -> None:
    """A unit admission created is indistinguishable from one enqueued through any other path."""
    admitted_dir = tmp_path / "admitted"
    enqueued_dir = tmp_path / "enqueued"
    admitted_dir.mkdir()
    enqueued_dir.mkdir()
    admitted_engine = _make_engine(admitted_dir)
    enqueued_engine = _make_engine(enqueued_dir)
    for engine in (admitted_engine, enqueued_engine):
        _seed_converted_sources(engine, 1)

    run_admission_pass(admitted_engine, contextualization_adapter(_config(admission_contextualization_enabled=True)))
    with Session(enqueued_engine) as session:
        enqueue_output(session, 1)
        session.commit()

    def _fields(unit: ContextualizationJob) -> tuple:
        return (unit.idempotency_key, unit.conversion_output_id, unit.source_id, unit.status, unit.attempts)

    (admitted_unit,) = _units(admitted_engine, ContextualizationJob)
    (enqueued_unit,) = _units(enqueued_engine, ContextualizationJob)
    assert _fields(admitted_unit) == _fields(enqueued_unit)


# --- capacity ---------------------------------------------------------------


def test_a_pass_stops_at_capacity_and_a_later_pass_admits_the_remainder(tmp_path: Path) -> None:
    """Admission fills the headroom, then resumes from the still-pending remainder once it drains."""
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 3)
    adapter = contextualization_adapter(
        _config(admission_contextualization_enabled=True, contextualization_queue_max_depth=2)
    )

    assert run_admission_pass(engine, adapter) == 2
    assert run_admission_pass(engine, adapter) == 0, "the stage is full, so nothing more is admitted"

    # Drain the backlog: succeeded units are no longer actionable, freeing headroom.
    with Session(engine) as session:
        for unit in session.exec(select(ContextualizationJob)).all():
            unit.status = WorkUnitStatus.SUCCEEDED
            session.add(unit)
        session.commit()

    assert run_admission_pass(engine, adapter) == 1
    assert {unit.conversion_output_id for unit in _units(engine, ContextualizationJob)} == {1, 2, 3}


def test_work_left_unadmitted_is_still_pending(tmp_path: Path) -> None:
    """The derivation has no memory of a pass, so what capacity excluded stays pending."""
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 3)
    adapter = contextualization_adapter(
        _config(admission_contextualization_enabled=True, contextualization_queue_max_depth=1)
    )

    run_admission_pass(engine, adapter)

    with Session(engine) as session:
        assert adapter.pending_work(session, None) == [2, 3]


# --- loop lifecycle ---------------------------------------------------------


def test_the_loop_admits_on_its_interval_and_stops_cleanly(tmp_path: Path) -> None:
    """The loop runs a pass, and stopping it joins the thread rather than leaving one behind."""
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 1)
    adapter = contextualization_adapter(_config(admission_contextualization_enabled=True))
    admitted = False

    with no_thread_leaks(action="raise"), AdmissionLoop(engine, adapter, interval_seconds=0.01):
        for _ in range(500):
            if _units(engine, ContextualizationJob):
                admitted = True
                break
            time.sleep(0.01)

    assert admitted, "the loop ran a pass without being driven"
    assert len(_units(engine, ContextualizationJob)) == 1


def test_stopping_the_loop_twice_is_harmless(tmp_path: Path) -> None:
    """A second stop is a no-op, so a shutdown path that stops defensively does not fail."""
    engine = _make_engine(tmp_path)

    with no_thread_leaks(action="raise"):
        loop = AdmissionLoop(engine, contextualization_adapter(_config()), interval_seconds=0.01)
        loop.start()
        loop.stop()
        loop.stop()
