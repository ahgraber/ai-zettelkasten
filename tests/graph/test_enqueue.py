"""Tests for the conversion-coupled enqueue resolvers and backfill target selection.

``enqueue_output`` / ``enqueue_backfill_outputs`` resolve the durable
``source_id`` from the conversion output and delegate to the domain enqueue
(dedupe on ``idempotency_key``, then the stage's declared capacity).
``latest_output_ids_per_source`` selects the corpus-scan target set a backfill
enqueues. FK enforcement is off, so standalone ``conversion_outputs`` rows
suffice alongside the work-unit table.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import Session, create_engine, select

from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.capacity import StageAtCapacityError
from aizk.graph.datamodel import ContextualizationJob
from aizk.graph.enqueue import (
    enqueue_backfill_outputs,
    enqueue_output,
    latest_output_ids_per_source,
    pending_contextualization_outputs,
)
from aizk.graph.markdown_source import ConversionOutputFreshness
from aizk.graph.workunit import enqueue_backfill
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

        job = enqueue_output(session, 42, queue_max_depth=0)
        session.commit()

        assert job.conversion_output_id == 42
        assert job.source_id == _UUID_A
        assert job.status is WorkUnitStatus.QUEUED


def test_enqueue_output_rejects_unknown_output(tmp_path: Path) -> None:
    """Enqueuing a locator with no conversion output is rejected, recording nothing."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="conversion output 7 not found"):
            enqueue_output(session, 7, queue_max_depth=0)
        assert session.exec(select(ContextualizationJob)).all() == []


def test_enqueue_backfill_resolves_each_and_dedupes(tmp_path: Path) -> None:
    """Backfill resolves and enqueues each output; re-enqueue reuses the open unit."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        session.commit()

        first = enqueue_backfill_outputs(session, [1, 2], confirmed=True, queue_max_depth=0)
        session.commit()
        # A second backfill overlapping the same outputs reuses the open units.
        second = enqueue_backfill_outputs(session, [1, 2], confirmed=True, queue_max_depth=0)
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
            enqueue_backfill_outputs(session, [1], queue_max_depth=0)
        assert session.exec(select(ContextualizationJob)).all() == [], "nothing is enqueued without confirmation"


def test_enqueue_output_refuses_new_work_at_capacity(tmp_path: Path) -> None:
    """At the declared capacity a new work-unit is refused and nothing is added."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        session.commit()

        enqueue_output(session, 1, queue_max_depth=1)
        session.commit()

        with pytest.raises(StageAtCapacityError, match="contextualization stage is at capacity"):
            enqueue_output(session, 2, queue_max_depth=1)
        session.rollback()

        assert len(session.exec(select(ContextualizationJob)).all()) == 1


def test_enqueue_output_at_capacity_returns_the_existing_unit(tmp_path: Path) -> None:
    """A duplicate bypasses the capacity check: reusing a unit adds no work to the backlog."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        session.commit()

        first = enqueue_output(session, 1, queue_max_depth=1)
        session.commit()

        again = enqueue_output(session, 1, queue_max_depth=1)
        session.commit()

        assert again.id == first.id
        assert len(session.exec(select(ContextualizationJob)).all()) == 1


def test_enqueue_output_without_a_declared_limit_accepts_work(tmp_path: Path) -> None:
    """A stage declaring no capacity limit enqueues without a capacity refusal."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        session.commit()

        enqueue_output(session, 1, queue_max_depth=0)
        enqueue_output(session, 2, queue_max_depth=0)
        session.commit()

        assert len(session.exec(select(ContextualizationJob)).all()) == 2


def test_enqueue_backfill_outputs_admits_only_the_batch_headroom(tmp_path: Path) -> None:
    """A bulk enqueue over more work than the headroom admits the headroom and leaves the rest."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        _add_output(session, output_id=2, source_id=_UUID_B)
        _add_output(session, output_id=3, source_id=_UUID_C)
        session.commit()

        admitted = enqueue_backfill_outputs(session, [1, 2, 3], confirmed=True, queue_max_depth=2)
        session.commit()

        assert [job.conversion_output_id for job in admitted] == [1, 2]
        assert {job.conversion_output_id for job in session.exec(select(ContextualizationJob)).all()} == {1, 2}


def test_enqueue_backfill_admits_only_the_batch_headroom(tmp_path: Path) -> None:
    """The domain bulk enqueue is bounded by the same batch headroom as its output-resolving wrapper."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        documents = [(1, _UUID_A), (2, _UUID_B), (3, _UUID_C)]

        admitted = enqueue_backfill(session, documents, confirmed=True, queue_max_depth=2)
        session.commit()

        assert [job.conversion_output_id for job in admitted] == [1, 2]
        assert {job.conversion_output_id for job in session.exec(select(ContextualizationJob)).all()} == {1, 2}


# --- pending-work derivation ------------------------------------------------


def test_the_pending_derivation_and_the_freshness_gate_pick_the_same_output(tmp_path: Path) -> None:
    """Admission and the execute-time gate agree on which output is current.

    Pinned against ids and timestamps that disagree, because that is when the two
    could diverge. A divergence is silent and unrecoverable: admission enqueues the
    output it thinks is current, the worker rejects it as superseded and writes
    nothing, and the source counts as covered from then on.
    """
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        # The lower id carries the later timestamp, so ordering by created_at and
        # ordering by id disagree about the winner.
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH + dt.timedelta(days=1))
        _add_output(session, output_id=2, source_id=_UUID_A, created_at=_EPOCH)
        session.commit()

        (pending_output_id,) = pending_contextualization_outputs(session)
        freshness = ConversionOutputFreshness()

        assert freshness.is_current(session, _UUID_A, pending_output_id), (
            "the output admission would enqueue is the one the worker accepts"
        )
        assert not freshness.is_current(session, _UUID_A, 1), "the superseded output is not current"
        assert pending_output_id == 2


def test_a_never_contextualized_source_is_pending(tmp_path: Path) -> None:
    """A source with a conversion output and no work-unit is work the stage owes."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        session.commit()

        assert pending_contextualization_outputs(session) == [1]


@pytest.mark.parametrize("status", list(WorkUnitStatus), ids=lambda status: status.value)
def test_a_source_whose_newest_output_has_a_unit_is_not_pending(tmp_path: Path, status: WorkUnitStatus) -> None:
    """A work-unit in any status covers its output, so the source is not pending."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A)
        session.add(
            ContextualizationJob(
                idempotency_key="conversion_output:1",
                conversion_output_id=1,
                source_id=_UUID_A,
                status=status,
            )
        )
        session.commit()

        assert pending_contextualization_outputs(session) == []


def test_a_re_converted_source_is_pending_again(tmp_path: Path) -> None:
    """A newer output is a distinct locator, so the unit covering the older one does not cover it."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_A, created_at=_EPOCH + dt.timedelta(days=1))
        session.add(
            ContextualizationJob(
                idempotency_key="conversion_output:1",
                conversion_output_id=1,
                source_id=_UUID_A,
                status=WorkUnitStatus.SUCCEEDED,
            )
        )
        session.commit()

        assert pending_contextualization_outputs(session) == [2]


def test_a_source_without_a_conversion_output_is_not_pending(tmp_path: Path) -> None:
    """An unconverted source has nothing to contextualize, so it never enters the pending set."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        assert pending_contextualization_outputs(session) == []


def test_the_contextualization_pending_set_is_a_function_of_state_alone(tmp_path: Path) -> None:
    """Two evaluations against identical state yield the identical set — the derivation has no memory."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_B, created_at=_EPOCH + dt.timedelta(days=1))
        session.commit()

        first = pending_contextualization_outputs(session)
        second = pending_contextualization_outputs(session)

        assert first == second == [1, 2]


def test_unadmitted_contextualization_work_stays_pending(tmp_path: Path) -> None:
    """Work a bounded evaluation left out is still pending on the next evaluation."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_B, created_at=_EPOCH + dt.timedelta(days=1))
        session.commit()

        assert pending_contextualization_outputs(session, limit=1) == [1]
        assert pending_contextualization_outputs(session) == [1, 2], "nothing records that an evaluation happened"


def test_the_contextualization_pending_limit_applies_after_the_anti_join(tmp_path: Path) -> None:
    """A bounded evaluation returns pending work, not a corpus sample that may hold none."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _add_output(session, output_id=1, source_id=_UUID_A, created_at=_EPOCH)
        _add_output(session, output_id=2, source_id=_UUID_B, created_at=_EPOCH + dt.timedelta(days=1))
        session.add(
            ContextualizationJob(
                idempotency_key="conversion_output:1",
                conversion_output_id=1,
                source_id=_UUID_A,
                status=WorkUnitStatus.QUEUED,
            )
        )
        session.commit()

        assert pending_contextualization_outputs(session, limit=1) == [2]


@pytest.mark.parametrize("limit", [0, -1])
def test_the_contextualization_pending_query_rejects_a_limit_that_does_not_bound(tmp_path: Path, limit: int) -> None:
    """A non-positive bound would widen the evaluation instead of narrowing it, so it is refused."""
    engine = _make_engine(tmp_path)
    with Session(engine) as session, pytest.raises(ValueError, match="limit must be a positive integer"):
        pending_contextualization_outputs(session, limit=limit)


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
