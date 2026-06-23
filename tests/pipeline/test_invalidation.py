"""Tests for lazy-invalidation staleness detection and the reprocessing confirmation gate.

Exercises the pipeline-identity rule that invalidation is lazy by default and large
reprocessing is gated behind explicit confirmation: a producer-version bump flags
the active generation stale without recomputing it, and a large-blast-radius
operation does not run until it is explicitly confirmed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from aizk.pipeline.invalidation import (
    ReprocessingConfirmationError,
    generation_is_stale,
    require_reprocessing_confirmation,
    stale_active_generations,
)
from aizk.pipeline.run import PipelineRun, RunStatus, record_run

_STAGE = "demo_stage"
_SCOPE = "11111111-1111-1111-1111-111111111111"


def _active(session: Session, stage: str) -> PipelineRun:
    """Return the single active run for a stage."""
    return session.exec(
        select(PipelineRun).where(PipelineRun.stage == stage, PipelineRun.status == RunStatus.ACTIVE)
    ).one()


def test_version_bump_marks_stale_no_eager_recompute(engine: Engine) -> None:
    """A producer-version bump flags the active generation stale without recomputing it."""
    with Session(engine) as session:
        run = record_run(
            session,
            stage=_STAGE,
            scope_id=_SCOPE,
            derivation_key="dk-v1",
            version_stamps={"producer_version": "1"},
        )
        session.commit()
        run_id = run.id

    # Bump the producer version to 2: detection is a read-only comparison.
    with Session(engine) as session:
        active = _active(session, _STAGE)
        assert generation_is_stale(active, version_field="producer_version", current_version=2) is True
        stale = stale_active_generations(session, stage=_STAGE, version_field="producer_version", current_version=2)
        assert [r.id for r in stale] == [run_id]

        # No eager recompute: the stale generation is still the one active run,
        # unmutated and usable — nothing was re-derived.
        runs = session.exec(select(PipelineRun).where(PipelineRun.stage == _STAGE)).all()
        assert len(runs) == 1
        assert runs[0].id == run_id
        assert runs[0].status is RunStatus.ACTIVE
        assert runs[0].derivation_key == "dk-v1"

    # At the current version the same generation is not stale.
    with Session(engine) as session:
        active = _active(session, _STAGE)
        assert generation_is_stale(active, version_field="producer_version", current_version=1) is False
        assert (
            stale_active_generations(session, stage=_STAGE, version_field="producer_version", current_version=1) == []
        )


def test_generation_without_the_version_stamp_is_not_stale(engine: Engine) -> None:
    """A run that recorded no producer version under the field is not flagged stale (nothing to compare)."""
    with Session(engine) as session:
        record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key="dk", version_stamps={"other": "9"})
        session.commit()
        active = _active(session, _STAGE)
        assert generation_is_stale(active, version_field="producer_version", current_version=2) is False


def test_large_reprocessing_requires_confirmation() -> None:
    """The confirmation gate refuses a large-blast-radius op until explicit approval is given."""
    with pytest.raises(ReprocessingConfirmationError, match="will not run until it is explicitly confirmed"):
        require_reprocessing_confirmation("corpus-wide backfill", confirmed=False)

    # With explicit approval it proceeds (returns None, raises nothing).
    assert require_reprocessing_confirmation("corpus-wide backfill", confirmed=True) is None
