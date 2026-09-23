"""Behavioral tests for admission (``aizk.graph.admission``).

Admission creates the work-units a stage's upstream state says should exist. A pass
admits nothing while the stage is switched off, nothing for a stage that declared no
derivation, nothing twice over unchanged state, and nothing beyond capacity.

Both stages run through their real adapters against real conversion outputs and
chunking runs, so a pass uses the same enqueue primitive every other path uses and
its units are comparable with theirs.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid4

from pyleak import no_thread_leaks
import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.console.stages.contextualization import DESCRIPTOR as CONTEXTUALIZATION_DESCRIPTOR
from aizk.console.stages.extraction import DESCRIPTOR as EXTRACTION_DESCRIPTOR
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.graph.admission import (
    AdmissionLoop,
    admission_adapter_for,
    contextualization_adapter,
    extraction_adapter,
    run_admission_pass,
)
from aizk.graph.chunks import CHUNKING_STAGE
from aizk.graph.config import AdmissionConfig
from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.graph.enqueue import enqueue_output
from aizk.graph.events import CONTEXTUALIZATION_STAGE, QueuedPayload as ContextualizationQueuedPayload
from aizk.graph.extraction_events import EXTRACTION_STAGE, QueuedPayload as ExtractionQueuedPayload
from aizk.graph.extraction_workunit import enqueue_extraction
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.lifecycle import MAX_BULK_SELECTION, WorkUnitStatus
from aizk.pipeline.run import PipelineRun, record_run

_UUID_A = UUID("11111111-1111-1111-1111-111111111111")
_UUID_B = UUID("22222222-2222-2222-2222-222222222222")
_UUID_C = UUID("33333333-3333-3333-3333-333333333333")

_EPOCH = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)

_SCHEMA_TABLES = [
    # ``sources`` carries the titles the console's pending listing resolves against.
    Source.__table__,
    ConversionOutput.__table__,
    ContextualizationJob.__table__,
    ExtractionJob.__table__,
    PipelineRun.__table__,
    PipelineEvent.__table__,
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
            record_run(session, stage=CHUNKING_STAGE, scope_id=source_id, derivation_key=f"dk-{source_id}")
        session.commit()


def _units(engine: Engine, model: type) -> list:
    """Return every work-unit row of a stage."""
    with Session(engine) as session:
        return list(session.exec(select(model)).all())


def _config(**overrides: object) -> AdmissionConfig:
    """Build hermetic admission settings, overriding only the named fields."""
    return AdmissionConfig(_env_file=None, **overrides)


class _CountingCursor(sqlite3.Cursor):
    """A cursor that tallies rows fetched into Python, on the connection it was opened from."""

    def fetchone(self):  # noqa: ANN201
        row = super().fetchone()
        if row is not None:
            self.connection.fetched_rows += 1
        return row

    def fetchmany(self, size: int = -1):  # noqa: ANN201
        rows = super().fetchmany(size)
        self.connection.fetched_rows += len(rows)
        return rows

    def fetchall(self):  # noqa: ANN201
        rows = super().fetchall()
        self.connection.fetched_rows += len(rows)
        return rows

    def __next__(self):  # noqa: ANN204
        row = super().__next__()
        self.connection.fetched_rows += 1
        return row


class _CountingConnection(sqlite3.Connection):
    """A sqlite3 connection whose cursors tally rows fetched into Python via ``fetched_rows``."""

    fetched_rows = 0

    def cursor(self, factory: type = _CountingCursor):  # noqa: ANN201
        """Return a :class:`_CountingCursor` bound to this connection."""
        return super().cursor(factory)


def _make_counting_engine(tmp_path: Path, name: str) -> Engine:
    """Create a file-based SQLite engine whose connections tally rows fetched into Python."""
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "factory": _CountingConnection},
    )
    SQLModel.metadata.create_all(engine, tables=_SCHEMA_TABLES)
    return engine


def _measure_cost(engine: Engine, adapter, limit: int) -> tuple[list, int, int]:
    """Run ``adapter.pending_work`` once and return its result plus (statement count, rows fetched).

    Statements are counted via ``after_cursor_execute``; rows are counted by the
    engine's counting cursor, reset just before the call so only this evaluation's
    cost is measured.
    """
    statements: list[str] = []

    def _record_statement(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    event.listen(engine, "after_cursor_execute", _record_statement)
    try:
        with Session(engine) as session:
            raw_connection = session.connection().connection.dbapi_connection
            raw_connection.fetched_rows = 0
            result = adapter.pending_work(session, limit)
            rows = raw_connection.fetched_rows
    finally:
        event.remove(engine, "after_cursor_execute", _record_statement)
    return result, len(statements), rows


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
        # _UUID_A already has a unit; _UUID_C is unchunked. Only _UUID_B is pending.
        enqueue_extraction(session, source_id=_UUID_A, queue_max_depth=0, origin="backfill", principal="test")
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
        enqueue_output(session, 1, queue_max_depth=0, origin="backfill", principal="test")
        session.commit()

    def _fields(unit: ContextualizationJob) -> tuple:
        return (unit.idempotency_key, unit.conversion_output_id, unit.source_id, unit.status, unit.attempts)

    (admitted_unit,) = _units(admitted_engine, ContextualizationJob)
    (enqueued_unit,) = _units(enqueued_engine, ContextualizationJob)
    assert _fields(admitted_unit) == _fields(enqueued_unit)


def test_an_admission_pass_records_one_creation_event_per_unit_with_origin_admission(tmp_path: Path) -> None:
    """Each unit an admission pass creates gets one ``queued`` event naming the admitting worker."""
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 2)

    run_admission_pass(engine, contextualization_adapter(_config(admission_contextualization_enabled=True)))

    expected_principal = f"worker:{os.getpid()}"
    with Session(engine) as session:
        units = _units(engine, ContextualizationJob)
        assert len(units) == 2
        for unit in units:
            events = session.exec(
                select(PipelineEvent).where(
                    PipelineEvent.work_unit_ref == str(unit.id), PipelineEvent.kind == "queued"
                )
            ).all()
            assert len(events) == 1
            payload = ContextualizationQueuedPayload.model_validate_json(events[0].payload_json)
            assert payload.origin == "admission"
            assert payload.principal == expected_principal


def test_an_extraction_admission_pass_records_creation_events_with_origin_admission(tmp_path: Path) -> None:
    """Extraction admission records the same creation-event contract as contextualization."""
    engine = _make_engine(tmp_path)
    _seed_chunked_sources(engine, _UUID_A)

    run_admission_pass(engine, extraction_adapter(_config(admission_extraction_enabled=True)))

    expected_principal = f"worker:{os.getpid()}"
    with Session(engine) as session:
        (unit,) = _units(engine, ExtractionJob)
        events = session.exec(
            select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(unit.id), PipelineEvent.kind == "queued")
        ).all()
        assert len(events) == 1
        payload = ExtractionQueuedPayload.model_validate_json(events[0].payload_json)
        assert payload.origin == "admission"
        assert payload.principal == expected_principal


# --- what a pass admits equals what observers are told ----------------------


def _reported_then_admitted(engine: Engine, descriptor, adapter, model: type) -> tuple[set[UUID], set[UUID]]:
    """Read the console-reported pending sources, then run an unconstrained pass.

    Returns the reported set and the sources the pass created units for, so the two
    can be compared as sets of the same thing.
    """
    with Session(engine) as session:
        reported = {UUID(row["source_id"]) for row in descriptor.pending_list(session, None)}

    before = {unit.source_id for unit in _units(engine, model)}
    run_admission_pass(engine, adapter)
    after = {unit.source_id for unit in _units(engine, model)}
    return reported, after - before


def test_the_reported_contextualization_pending_set_is_what_a_pass_admits(tmp_path: Path) -> None:
    """What the console says is behind contextualization is exactly what a pass takes up.

    The count and listing an operator reads and the work an admission pass creates
    resolve through one derivation, so the operator surface cannot disagree with
    what admission will actually do.
    """
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 3)
    with Session(engine) as session:
        # One source is already covered, so the pending set is a strict subset of the corpus.
        enqueue_output(session, 1, queue_max_depth=0, origin="backfill", principal="test")
        session.commit()

    reported, admitted = _reported_then_admitted(
        engine,
        CONTEXTUALIZATION_DESCRIPTOR,
        contextualization_adapter(_config(admission_contextualization_enabled=True)),
        ContextualizationJob,
    )

    assert len(reported) == 2, "the corpus must hold pending work for the comparison to mean anything"
    assert reported == admitted
    with Session(engine) as session:
        assert CONTEXTUALIZATION_DESCRIPTOR.pending_list(session, None) == []


def test_the_reported_extraction_pending_set_is_what_a_pass_admits(tmp_path: Path) -> None:
    """What the console says is behind extraction is exactly what a pass takes up."""
    engine = _make_engine(tmp_path)
    _seed_chunked_sources(engine, _UUID_A, _UUID_B, _UUID_C)
    with Session(engine) as session:
        # One source is already covered, so the pending set is a strict subset of the corpus.
        enqueue_extraction(session, source_id=_UUID_A, queue_max_depth=0, origin="backfill", principal="test")
        session.commit()

    reported, admitted = _reported_then_admitted(
        engine,
        EXTRACTION_DESCRIPTOR,
        extraction_adapter(_config(admission_extraction_enabled=True)),
        ExtractionJob,
    )

    assert reported == {_UUID_B, _UUID_C}
    assert reported == admitted
    with Session(engine) as session:
        assert EXTRACTION_DESCRIPTOR.pending_list(session, None) == []


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


# --- per-pass ceiling ---------------------------------------------------------


def test_an_unlimited_stage_admits_a_bounded_batch_per_pass(tmp_path: Path) -> None:
    """A stage with no declared capacity still bounds one pass at the per-pass ceiling."""
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, MAX_BULK_SELECTION + 1)
    adapter = contextualization_adapter(_config(admission_contextualization_enabled=True))

    admitted = run_admission_pass(engine, adapter)

    assert admitted == MAX_BULK_SELECTION
    assert len(_units(engine, ContextualizationJob)) == MAX_BULK_SELECTION
    with Session(engine) as session:
        assert len(adapter.pending_work(session, None)) == 1, "the remainder stays pending"


def test_repeated_passes_drain_an_unlimited_stage_to_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no capacity constraint, repeated bounded passes still empty the pending set."""
    monkeypatch.setattr("aizk.pipeline.admission.MAX_BULK_SELECTION", 3)
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 10)
    adapter = contextualization_adapter(_config(admission_contextualization_enabled=True))

    total_admitted = 0
    passes_run = 0
    admitted = -1
    while admitted != 0:
        admitted = run_admission_pass(engine, adapter)
        total_admitted += admitted
        passes_run += 1

    assert total_admitted == 10
    assert passes_run == 5, "four batches of 3 (the last partial) plus a final pass admitting nothing"
    with Session(engine) as session:
        assert adapter.pending_work(session, None) == []


@pytest.mark.parametrize(
    ("queue_max_depth", "expected_admitted"),
    [
        pytest.param(10, 3, id="headroom-above-ceiling"),
        pytest.param(2, 2, id="headroom-below-ceiling"),
    ],
)
def test_a_pass_admits_the_smaller_of_ceiling_and_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queue_max_depth: int, expected_admitted: int
) -> None:
    """A pass is bounded by the per-pass ceiling when headroom exceeds it, and by headroom otherwise."""
    monkeypatch.setattr("aizk.pipeline.admission.MAX_BULK_SELECTION", 3)
    engine = _make_engine(tmp_path)
    _seed_converted_sources(engine, 8)
    adapter = contextualization_adapter(
        _config(admission_contextualization_enabled=True, contextualization_queue_max_depth=queue_max_depth)
    )

    assert run_admission_pass(engine, adapter) == expected_admitted


_PENDING_SOURCE_IDS = [UUID(f"{index:08d}-dddd-dddd-dddd-dddddddddddd") for index in range(1, 6)]


def _seed_contextualization_cost_corpus(engine: Engine, *, admitted_count: int) -> None:
    """Seed the shared pending outputs plus ``admitted_count`` already-covered outputs."""
    with Session(engine) as session:
        for index, source_id in enumerate(_PENDING_SOURCE_IDS, start=1):
            _add_output(session, output_id=index, source_id=source_id, created_at=_EPOCH + dt.timedelta(days=index))
        for index in range(admitted_count):
            _add_output(
                session,
                output_id=1000 + index,
                source_id=uuid4(),
                created_at=_EPOCH + dt.timedelta(days=1000 + index),
            )
        session.commit()
    with Session(engine) as session:
        for index in range(admitted_count):
            enqueue_output(session, 1000 + index, queue_max_depth=0, origin="backfill", principal="test")
        session.commit()


def _seed_extraction_cost_corpus(engine: Engine, *, admitted_count: int) -> None:
    """Seed the shared pending sources plus ``admitted_count`` already-covered sources."""
    admitted_ids = [uuid4() for _ in range(admitted_count)]
    _seed_chunked_sources(engine, *_PENDING_SOURCE_IDS, *admitted_ids)
    with Session(engine) as session:
        for source_id in admitted_ids:
            enqueue_extraction(session, source_id=source_id, queue_max_depth=0, origin="backfill", principal="test")
        session.commit()


@pytest.mark.parametrize(
    ("adapter_factory", "seed"),
    [
        pytest.param(contextualization_adapter, _seed_contextualization_cost_corpus, id="contextualization"),
        pytest.param(extraction_adapter, _seed_extraction_cost_corpus, id="extraction"),
    ],
)
def test_a_bounded_pending_evaluation_does_not_scale_with_the_admitted_corpus(
    tmp_path: Path, adapter_factory, seed
) -> None:
    """Two corpora sharing the same pending set cost the same, however many sources are already admitted."""
    small_engine = _make_counting_engine(tmp_path, "small.db")
    large_engine = _make_counting_engine(tmp_path, "large.db")
    seed(small_engine, admitted_count=5)
    seed(large_engine, admitted_count=200)
    adapter = adapter_factory(_config(admission_contextualization_enabled=True, admission_extraction_enabled=True))

    small_result, small_statements, small_rows = _measure_cost(small_engine, adapter, len(_PENDING_SOURCE_IDS))
    large_result, large_statements, large_rows = _measure_cost(large_engine, adapter, len(_PENDING_SOURCE_IDS))

    assert small_result == large_result
    assert (small_statements, small_rows) == (large_statements, large_rows)


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
