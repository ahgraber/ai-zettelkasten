"""Tests for the stage-run / dataset-version primitive (atomic supersession).

Covers the spec requirement that a stage's derived outputs belong to a run
identified by ``(stage, scope_id)`` with at most one active run per scope,
invalidated atomically: recording a new run and superseding the prior happen in
one transaction, never leaving two active runs nor a window with none.

Also covers the read-side helpers over the run table: the active-run lookup,
the active-run requirement a recorder places on a run id, and the idempotent
applicability insert, exercised over a throwaway applicability table on a
private ``MetaData`` so no stage table is involved.
"""

from __future__ import annotations

from pathlib import Path
import threading
from uuid import UUID

from pyleak import no_thread_leaks
import pytest
from sqlalchemy import Engine, MetaData, create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Field, Session, SQLModel, func, select

from aizk.pipeline.run import (
    PipelineRun,
    RunStatus,
    active_run,
    record_applicability,
    record_run,
    require_active_run,
)

_STAGE = "contextualization"
_SCOPE = UUID("11111111-1111-1111-1111-111111111111")


class _PrivateBase(SQLModel):
    """A declarative base on its own ``MetaData`` so the stub table never joins ``SQLModel.metadata``."""

    metadata = MetaData()


class StubApplicability(_PrivateBase, table=True):
    """A stage-shaped applicability relation: a composite key, a fingerprint, and one extra column."""

    __tablename__ = "stub_run_applicability"
    run_id: int = Field(primary_key=True)
    upstream_id: str = Field(primary_key=True)
    input_fingerprint: str = Field(nullable=False)
    policy: str = Field(nullable=False)


@pytest.fixture
def applicability_engine(tmp_path: Path) -> Engine:
    """A SQLite engine holding only the stub applicability table."""
    eng = create_engine(f"sqlite:///{tmp_path / 'applicability.db'}")
    _PrivateBase.metadata.create_all(eng)
    yield eng
    eng.dispose()


def _active_runs(session: Session, stage: str, scope_id: UUID) -> list[PipelineRun]:
    """Return active runs for a scope, newest first."""
    return list(
        session.exec(
            select(PipelineRun)
            .where(
                PipelineRun.stage == stage,
                PipelineRun.scope_id == scope_id,
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
        first = record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp-1")
        session.commit()
        first_id = first.id
        first_derivation_key = first.derivation_key

    with Session(engine) as session:
        second = record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp-2")
        session.commit()
        second_id = second.id
        assert second.supersedes_run_id == first_id

    with Session(engine) as session:
        active = _active_runs(session, _STAGE, _SCOPE)
        assert [r.id for r in active] == [second_id], "exactly the new run is active"

        prior = session.get(PipelineRun, first_id)
        assert prior.status is RunStatus.SUPERSEDED
        assert prior.derivation_key == first_derivation_key, "prior run's recorded outputs are unmodified"


def test_supersession_is_scoped_per_stage_scope_id(engine: Engine) -> None:
    """One active run per ``(stage, scope_id)`` — a different scope stays active."""
    other_scope = UUID("22222222-2222-2222-2222-222222222222")
    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="a")
        record_run(session, stage=_STAGE, scope_id=other_scope, derivation_key="b")
        session.commit()

    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="a2")
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
        first = record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp-1")
        session.commit()
        first_id = first.id

    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp-2")
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
        record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="initial")
        session.commit()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _attempt(derivation_key: str) -> None:
        barrier.wait()
        try:
            with Session(serialized_engine) as session:
                record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key=derivation_key)
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
        assert active[0].derivation_key != "initial", "the active run is one of the new attempts"


def test_status_stored_as_lowercase_value(engine: Engine) -> None:
    """The raw stored status value is 'active', not the enum name 'ACTIVE'.

    The partial unique index predicate ``status = 'active'`` depends on this.
    If SQLAlchemy's Enum type were used instead of String, it would store the
    member name ('ACTIVE') and the index would never match.
    """
    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp")
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
                "INSERT INTO pipeline_runs (stage, scope_id, status, derivation_key,"
                " version_stamps_json, created_at)"
                " VALUES (:stage, :scope, 'active', 'fp-1', '{}', :now)"
            ),
            {"stage": _STAGE, "scope": _SCOPE.hex, "now": now},
        )
        conn.commit()

    with pytest.raises(IntegrityError), engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO pipeline_runs (stage, scope_id, status, derivation_key,"
                " version_stamps_json, created_at)"
                " VALUES (:stage, :scope, 'active', 'fp-2', '{}', :now)"
            ),
            {"stage": _STAGE, "scope": _SCOPE.hex, "now": now},
        )
        conn.commit()


def test_version_stamps_json_escapes_non_ascii(engine: Engine) -> None:
    """The stored ``version_stamps_json`` is compact, key-sorted, ASCII-escaped JSON.

    The envelope is part of the run's stored identity, so a non-ASCII stamp value
    is escaped rather than written raw.
    """
    with Session(engine) as session:
        record_run(
            session,
            stage=_STAGE,
            scope_id=_SCOPE,
            derivation_key="fp",
            version_stamps={"model": "Zürich", "context": "v1"},
        )
        session.commit()

    with engine.connect() as conn:
        raw = conn.execute(text("SELECT version_stamps_json FROM pipeline_runs")).scalar()
    assert raw == '{"context":"v1","model":"Z\\u00fcrich"}'


# --------------------------------------------------------------------------- #
# active_run
# --------------------------------------------------------------------------- #


def test_active_run_returns_the_scope_active_run(engine: Engine) -> None:
    """After a supersession the lookup returns the new active run, not the superseded one."""
    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp-1")
        second = record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp-2")
        session.commit()
        second_id = second.id

    with Session(engine) as session:
        found = active_run(session, _STAGE, _SCOPE)
        assert found is not None
        assert found.id == second_id
        assert found.status is RunStatus.ACTIVE


def test_active_run_is_none_when_the_scope_has_only_superseded_runs(engine: Engine) -> None:
    """A scope whose every run is superseded has no active run."""
    with Session(engine) as session:
        run = record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp-1")
        run.status = RunStatus.SUPERSEDED
        session.add(run)
        session.commit()

    with Session(engine) as session:
        assert active_run(session, _STAGE, _SCOPE) is None


# --------------------------------------------------------------------------- #
# require_active_run
# --------------------------------------------------------------------------- #


def test_require_active_run_returns_the_active_run(engine: Engine) -> None:
    """An active run of the named stage is returned by id."""
    with Session(engine) as session:
        run = record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp-1")
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        found = require_active_run(session, run_id, _STAGE, label="variant run")
        assert found.id == run_id
        assert found.stage == _STAGE


def _seed_superseded_run(session: Session) -> int:
    """Record a run for the test scope and supersede it; return its id."""
    run = record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="fp-1")
    run.status = RunStatus.SUPERSEDED
    session.add(run)
    session.commit()
    return run.id


def _seed_other_stage_run(session: Session) -> int:
    """Record an active run under a different stage for the test scope; return its id."""
    run = record_run(session, stage="chunking", scope_id=_SCOPE, derivation_key="fp-1")
    session.commit()
    return run.id


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(lambda session: 999_999, id="missing"),
        pytest.param(_seed_superseded_run, id="superseded"),
        pytest.param(_seed_other_stage_run, id="other-stage"),
    ],
)
def test_require_active_run_refuses_a_run_that_is_not_active_for_the_stage(engine: Engine, seed) -> None:  # noqa: ANN001
    """A missing id, a superseded run, and an active run of another stage are refused with one message."""
    with Session(engine) as session:
        run_id = seed(session)

    with Session(engine) as session, pytest.raises(ValueError) as excinfo:
        require_active_run(session, run_id, _STAGE, label="variant run")

    assert str(excinfo.value) == (
        f"variant run {run_id!r} is not an active variant run: it is missing, not active, or not a variant run"
    )


# --------------------------------------------------------------------------- #
# record_applicability
# --------------------------------------------------------------------------- #

_KEY = {"run_id": 7, "upstream_id": "upstream-1"}


def test_record_applicability_inserts_a_new_row(applicability_engine: Engine) -> None:
    """A key with no row stages one carrying the key, the fingerprint, and the extra field."""
    with Session(applicability_engine) as session:
        row = record_applicability(session, StubApplicability, _KEY, input_fingerprint="fp-a", policy="raw")
        assert row in session.new, "the row is staged, not committed, by the primitive"
        session.commit()

    with Session(applicability_engine) as session:
        stored = session.exec(select(StubApplicability)).one()
    assert (stored.run_id, stored.upstream_id) == (7, "upstream-1")
    assert stored.input_fingerprint == "fp-a"
    assert stored.policy == "raw"


def test_record_applicability_returns_the_existing_row_unchanged(applicability_engine: Engine) -> None:
    """A repeat with the same fingerprint and fields returns the stored row and inserts nothing."""
    with Session(applicability_engine) as session:
        record_applicability(session, StubApplicability, _KEY, input_fingerprint="fp-a", policy="raw")
        session.commit()

    with Session(applicability_engine) as session:
        existing = session.get(StubApplicability, _KEY)
        again = record_applicability(session, StubApplicability, _KEY, input_fingerprint="fp-a", policy="raw")
        assert again is existing
        session.commit()
        total = session.exec(select(func.count()).select_from(StubApplicability)).one()
    assert total == 1


@pytest.mark.parametrize(
    ("input_fingerprint", "policy"),
    [
        pytest.param("fp-b", "raw", id="fingerprint"),
        pytest.param("fp-a", "contextualized", id="extra-field"),
    ],
)
def test_record_applicability_refuses_a_conflicting_row(
    applicability_engine: Engine, input_fingerprint: str, policy: str
) -> None:
    """A repeat whose fingerprint or extra field differs is refused and the stored row is untouched."""
    with Session(applicability_engine) as session:
        record_applicability(session, StubApplicability, _KEY, input_fingerprint="fp-a", policy="raw")
        session.commit()

    with Session(applicability_engine) as session:
        with pytest.raises(ValueError, match="different input_fingerprint"):
            record_applicability(session, StubApplicability, _KEY, input_fingerprint=input_fingerprint, policy=policy)
        stored = session.get(StubApplicability, _KEY)
        assert (stored.input_fingerprint, stored.policy) == ("fp-a", "raw")
