"""Tests for the stage-run / dataset-version primitive (atomic supersession).

Covers the spec requirement that a stage's derived outputs belong to a run
identified by ``(stage, scope_key)`` with at most one active run per scope,
invalidated atomically: recording a new run and superseding the prior happen in
one transaction, never leaving two active runs nor a window with none.
"""

from __future__ import annotations

import threading

from pyleak import no_thread_leaks
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, func, select

from aizk.pipeline.run import PipelineRun, RunStatus, record_run

_STAGE = "contextualization"
_SCOPE = "document:abc"


def _active_runs(session: Session, stage: str, scope_key: str) -> list[PipelineRun]:
    """Return active runs for a scope, newest first."""
    return list(
        session.exec(
            select(PipelineRun)
            .where(
                PipelineRun.stage == stage,
                PipelineRun.scope_key == scope_key,
                PipelineRun.status == RunStatus.ACTIVE,
            )
            .order_by(PipelineRun.id.desc())
        )
    )


def test_atomic_supersede(engine: Engine) -> None:
    """A new run becomes active and the prior becomes superseded, prior outputs intact.

    The prior run row is unchanged except for its status flip — superseding is a
    pure status transition, leaving the prior run's recorded outputs untouched.
    """
    with Session(engine) as session:
        first = record_run(session, stage=_STAGE, scope_key=_SCOPE, input_fingerprint="fp-1")
        session.commit()
        first_id = first.id
        first_fingerprint = first.input_fingerprint

    with Session(engine) as session:
        second = record_run(session, stage=_STAGE, scope_key=_SCOPE, input_fingerprint="fp-2")
        session.commit()
        second_id = second.id
        assert second.supersedes_run_id == first_id

    with Session(engine) as session:
        active = _active_runs(session, _STAGE, _SCOPE)
        assert [r.id for r in active] == [second_id], "exactly the new run is active"

        prior = session.get(PipelineRun, first_id)
        assert prior.status is RunStatus.SUPERSEDED
        assert prior.input_fingerprint == first_fingerprint, "prior run's recorded outputs are unmodified"


def test_supersession_is_scoped_per_stage_scope_key(engine: Engine) -> None:
    """One active run per ``(stage, scope_key)`` — a different scope stays active."""
    other_scope = "document:xyz"
    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_key=_SCOPE, input_fingerprint="a")
        record_run(session, stage=_STAGE, scope_key=other_scope, input_fingerprint="b")
        session.commit()

    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_key=_SCOPE, input_fingerprint="a2")
        session.commit()

    with Session(engine) as session:
        assert len(_active_runs(session, _STAGE, _SCOPE)) == 1
        assert len(_active_runs(session, _STAGE, other_scope)) == 1, "untouched scope keeps its active run"


def test_failed_supersession_changes_nothing(engine: Engine) -> None:
    """A new-run transaction that fails before commit leaves the prior run active.

    ``record_run`` flushes (demote + insert) but never commits; rolling back the
    caller's transaction undoes both, so the prior run is still active and no
    partial new run is present.
    """
    with Session(engine) as session:
        first = record_run(session, stage=_STAGE, scope_key=_SCOPE, input_fingerprint="fp-1")
        session.commit()
        first_id = first.id

    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_key=_SCOPE, input_fingerprint="fp-2")
        session.rollback()  # transaction fails before commit

    with Session(engine) as session:
        total = session.exec(select(func.count()).select_from(PipelineRun)).one()
        assert total == 1, "no partial new run persisted"
        active = _active_runs(session, _STAGE, _SCOPE)
        assert [r.id for r in active] == [first_id], "prior run is still active"


def test_concurrent_runs_one_active(serialized_engine: Engine) -> None:
    """Two concurrent attempts to open a run for one scope leave exactly one active.

    Under the single serialized writer (``BEGIN IMMEDIATE``) plus the partial
    unique index, one attempt wins; the other either supersedes-then-inserts
    cleanly or is rejected — either way exactly one run is active afterward and
    no run beyond the active one is left active.
    """
    with Session(serialized_engine) as session:
        record_run(session, stage=_STAGE, scope_key=_SCOPE, input_fingerprint="initial")
        session.commit()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _attempt(fingerprint: str) -> None:
        barrier.wait()
        try:
            with Session(serialized_engine) as session:
                record_run(session, stage=_STAGE, scope_key=_SCOPE, input_fingerprint=fingerprint)
                session.commit()
        except (IntegrityError, OperationalError) as exc:  # lost the race; expected
            errors.append(exc)

    with no_thread_leaks(action="raise"):
        threads = [threading.Thread(target=_attempt, args=(f"fp-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    with Session(serialized_engine) as session:
        active = _active_runs(session, _STAGE, _SCOPE)
        assert len(active) == 1, "exactly one active run after concurrent attempts"
        assert active[0].input_fingerprint != "initial", "the active run is one of the new attempts"


def test_status_stored_as_lowercase_value(engine: Engine) -> None:
    """The raw stored status value is 'active', not the enum name 'ACTIVE'.

    The partial unique index predicate ``status = 'active'`` depends on this.
    If SQLAlchemy's Enum type were used instead of String, it would store the
    member name ('ACTIVE') and the index would never match.
    """
    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_key=_SCOPE, input_fingerprint="fp")
        session.commit()

    with engine.connect() as conn:
        raw = conn.execute(text("SELECT status FROM pipeline_runs LIMIT 1")).scalar()
    assert raw == "active", f"expected 'active', got {raw!r}"


def test_direct_active_insert_violates_unique_index(engine: Engine) -> None:
    """Two rows with status='active' for the same scope violate the partial unique index."""
    now = "2026-01-01T00:00:00"
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO pipeline_runs (stage, scope_key, status, input_fingerprint,"
                " version_stamps_json, created_at)"
                " VALUES (:stage, :scope, 'active', 'fp-1', '{}', :now)"
            ),
            {"stage": _STAGE, "scope": _SCOPE, "now": now},
        )
        conn.commit()

    with pytest.raises(IntegrityError), engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO pipeline_runs (stage, scope_key, status, input_fingerprint,"
                " version_stamps_json, created_at)"
                " VALUES (:stage, :scope, 'active', 'fp-2', '{}', :now)"
            ),
            {"stage": _STAGE, "scope": _SCOPE, "now": now},
        )
        conn.commit()
