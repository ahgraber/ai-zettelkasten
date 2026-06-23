"""Tests for the pipeline-identity foundation: the semantic derivation key and run-level reuse.

Covers the pipeline-identity contract that a derivation key is a function of
semantic inputs only (so it is portable across backends), that it embeds upstream
keys so an upstream change propagates downstream, and that re-invoking a stage
whose derivation key is unchanged reuses the active run rather than superseding it.
"""

from __future__ import annotations

from sqlalchemy import Engine
from sqlmodel import Session, func, select

from aizk.pipeline.identity import derivation_key
from aizk.pipeline.run import PipelineRun, RunStatus, record_run, reuse_or_record_run

_STAGE = "chunking"
_SCOPE = "source:abc"


def test_derivation_key_excludes_db_local_ids() -> None:
    """Equal semantic inputs compute an equal key — the portability proxy.

    The helper accepts no run id or surrogate, so two backends whose local row and
    run ids differ compute byte-identical keys from identical semantic inputs.
    Building the inputs in a different insertion order guards against an accidental
    dependence on ordering rather than content; the inequality on a changed field
    guards against a vacuous constant-function false positive.
    """
    backend_a = derivation_key(
        inputs={"markdown_hash": "deadbeef", "splitter_version": 3},
        upstream_keys=["upstream-key-1"],
    )
    backend_b = derivation_key(
        inputs={"splitter_version": 3, "markdown_hash": "deadbeef"},
        upstream_keys=["upstream-key-1"],
    )
    assert backend_a == backend_b, "equal semantic inputs must compute an equal key regardless of local state"

    different_content = derivation_key(
        inputs={"markdown_hash": "feedface", "splitter_version": 3},
        upstream_keys=["upstream-key-1"],
    )
    assert different_content != backend_a, "a changed semantic input must change the key (not a constant function)"


def test_derivation_key_propagates_upstream_change() -> None:
    """Changing an upstream key flips the downstream key; an unchanged upstream does not."""
    inputs = {"content_hash": "c0ffee", "context_version": 2}
    base = derivation_key(inputs=inputs, upstream_keys=["chunking-key-v1"])
    unchanged = derivation_key(inputs=inputs, upstream_keys=["chunking-key-v1"])
    changed = derivation_key(inputs=inputs, upstream_keys=["chunking-key-v2"])

    assert unchanged == base, "an unchanged upstream key reuses the same downstream key"
    assert changed != base, "an upstream change must propagate to the downstream key"


def test_run_level_idempotency_reuse(engine: Engine) -> None:
    """Re-invoking a stage with an unchanged derivation key reuses the active run.

    Run-level idempotency: a matching key returns the existing active run with no
    new run and no supersession; a changed key records a new active run that
    supersedes the prior, leaving exactly one active.
    """
    key = derivation_key(inputs={"markdown_hash": "abc123", "splitter_version": 1})
    with Session(engine) as session:
        first = record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key=key)
        session.commit()
        first_id = first.id

    with Session(engine) as session:
        reused = reuse_or_record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key=key)
        session.commit()
        assert reused.id == first_id, "an unchanged derivation key reuses the active run"
        total = session.exec(select(func.count()).select_from(PipelineRun)).one()
        assert total == 1, "no new run is recorded on reuse"

    changed_key = derivation_key(inputs={"markdown_hash": "abc123", "splitter_version": 2})
    with Session(engine) as session:
        new_run = reuse_or_record_run(session, stage=_STAGE, scope_id=_SCOPE, derivation_key=changed_key)
        session.commit()
        assert new_run.id != first_id, "a changed derivation key records a new run"
        assert new_run.supersedes_run_id == first_id, "the new run supersedes the prior active run"
        active = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == _STAGE,
                PipelineRun.scope_id == _SCOPE,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).all()
        assert len(active) == 1, "exactly one active run remains"
