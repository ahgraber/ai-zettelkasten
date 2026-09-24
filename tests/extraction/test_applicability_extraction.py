"""Validation tests for ``aizk.graph.extraction_run.record_extraction_run_applicability``.

One test per validation clause: the run must exist, be active, and be an
extraction run; the upstream run must exist, be a chunking or
contextualization run, and share the extraction run's scope; the candidate key
recomputed from the upstream's persisted output and the currently-resolved
configuration must equal the run's derivation key; the run's
``ExtractionRunInput`` must exist; and an identical relation is a no-op while a
conflicting one (same primary key, different fields) is rejected — there is no
update path.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.graph.chunks import CHUNKING_STAGE
from aizk.graph.contextualization import VARIANT_STAGE
from aizk.graph.datamodel import (
    ChunkRunInput,
    ContextualizationRunInput,
    ExtractionRunApplicability,
    ExtractionRunInput,
)
from aizk.graph.extraction_run import extraction_derivation_key, record_extraction_run_applicability
from aizk.graph.mentions import MENTION_EXTRACTION_STAGE, ResolvedExtractionConfig
from aizk.pipeline.run import PipelineRun, RunStatus, record_run

_SOURCE_A = UUID("11111111-1111-1111-1111-111111111111")
_SOURCE_B = UUID("22222222-2222-2222-2222-222222222222")

_RESOLVED = ResolvedExtractionConfig(extractor_version="stub/v1", materializer_version="1", input_policy="raw")

_SCHEMA_TABLES = [
    ChunkRunInput.__table__,
    ContextualizationRunInput.__table__,
    ExtractionRunApplicability.__table__,
    ExtractionRunInput.__table__,
    PipelineRun.__table__,
]


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """A file-based SQLite engine carrying only the tables this module touches."""
    sqlite_engine = create_engine(
        f"sqlite:///{tmp_path / 'applicability.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(sqlite_engine, tables=_SCHEMA_TABLES)
    return sqlite_engine


@pytest.fixture
def session(engine: Engine) -> Session:
    """An open session on the per-test engine."""
    return Session(engine)


def _seed_chunking_upstream(
    session: Session, *, source_id: UUID = _SOURCE_A, output_fingerprint: str = "chunk-output-fp"
) -> PipelineRun:
    """Insert and flush an active chunking run with a ``ChunkRunInput`` carrying ``output_fingerprint``."""
    run = PipelineRun(
        stage=CHUNKING_STAGE, scope_id=source_id, status=RunStatus.ACTIVE, derivation_key=f"dk-{uuid4()}"
    )
    session.add(run)
    session.flush()
    session.add(
        ChunkRunInput(
            run_id=run.id,
            conversion_output_id="co-1",
            markdown_hash_xx64="dummy",
            chunk_set_fingerprint=output_fingerprint,
        )
    )
    session.flush()
    return run


def _seed_extraction_run(
    session: Session,
    *,
    source_id: UUID = _SOURCE_A,
    status: RunStatus = RunStatus.ACTIVE,
    stage: str = MENTION_EXTRACTION_STAGE,
    derivation_key: str,
    with_run_input: bool = True,
) -> PipelineRun:
    """Insert and flush a (by default active, extraction-stage) run, optionally with its ``ExtractionRunInput``."""
    run = PipelineRun(stage=stage, scope_id=source_id, status=status, derivation_key=derivation_key)
    session.add(run)
    session.flush()
    if with_run_input:
        session.add(
            ExtractionRunInput(
                run_id=run.id,
                upstream_run_id=-1,
                input_policy="raw",
                input_fingerprint="unused",
                config_fingerprint="unused",
            )
        )
        session.flush()
    return run


def _matching_extraction_run(session: Session, *, upstream: PipelineRun, source_id: UUID = _SOURCE_A) -> PipelineRun:
    """Seed an active extraction run whose derivation key matches ``upstream``'s output under ``_RESOLVED``."""
    chunk_input = session.get(ChunkRunInput, upstream.id)
    key = extraction_derivation_key(
        input_fingerprint=chunk_input.chunk_set_fingerprint, config_fingerprint=_RESOLVED.config_fingerprint()
    )
    return _seed_extraction_run(session, source_id=source_id, derivation_key=key)


# --------------------------------------------------------------------------- #
# Clause 1 — the run exists, is active, and is an extraction run
# --------------------------------------------------------------------------- #


def test_rejects_a_missing_run(session: Session) -> None:
    """A ``run_id`` that resolves to no row is rejected."""
    upstream = _seed_chunking_upstream(session)

    with pytest.raises(ValueError, match="not an active"):
        record_extraction_run_applicability(
            session, run_id=999999, upstream_run_id=upstream.id, input_policy="raw", resolved=_RESOLVED
        )


def test_rejects_a_superseded_run(session: Session) -> None:
    """A run that is no longer active is rejected."""
    upstream = _seed_chunking_upstream(session)
    run = _matching_extraction_run(session, upstream=upstream)
    run.status = RunStatus.SUPERSEDED
    session.add(run)
    session.flush()

    with pytest.raises(ValueError, match="not an active"):
        record_extraction_run_applicability(
            session, run_id=run.id, upstream_run_id=upstream.id, input_policy="raw", resolved=_RESOLVED
        )


def test_rejects_a_run_that_is_not_an_extraction_run(session: Session) -> None:
    """A run belonging to a different stage is rejected, even if active."""
    upstream = _seed_chunking_upstream(session)
    other_stage_run = _seed_extraction_run(
        session, source_id=_SOURCE_B, stage=CHUNKING_STAGE, derivation_key="dk-other-stage", with_run_input=False
    )

    with pytest.raises(ValueError, match="not an active"):
        record_extraction_run_applicability(
            session, run_id=other_stage_run.id, upstream_run_id=upstream.id, input_policy="raw", resolved=_RESOLVED
        )


# --------------------------------------------------------------------------- #
# Clause 2 — the upstream run exists, is a chunking/contextualization run, and shares scope_id
# --------------------------------------------------------------------------- #


def test_rejects_a_missing_upstream_run(session: Session) -> None:
    """An ``upstream_run_id`` that resolves to no row is rejected."""
    upstream = _seed_chunking_upstream(session)
    run = _matching_extraction_run(session, upstream=upstream)

    with pytest.raises(ValueError, match="does not resolve to a chunking or contextualization run"):
        record_extraction_run_applicability(
            session, run_id=run.id, upstream_run_id=999999, input_policy="raw", resolved=_RESOLVED
        )


def test_rejects_an_upstream_run_of_the_wrong_stage(session: Session) -> None:
    """An upstream run that is neither a chunking nor a contextualization run is rejected."""
    upstream = _seed_chunking_upstream(session)
    run = _matching_extraction_run(session, upstream=upstream)
    wrong_stage_run = PipelineRun(
        stage=MENTION_EXTRACTION_STAGE, scope_id=_SOURCE_B, status=RunStatus.ACTIVE, derivation_key="dk-wrong-stage"
    )
    session.add(wrong_stage_run)
    session.flush()

    with pytest.raises(ValueError, match="does not resolve to a chunking or contextualization run"):
        record_extraction_run_applicability(
            session, run_id=run.id, upstream_run_id=wrong_stage_run.id, input_policy="raw", resolved=_RESOLVED
        )


def test_rejects_an_upstream_run_of_a_different_scope(session: Session) -> None:
    """An upstream run belonging to a different source than the extraction run's scope is rejected."""
    upstream = _seed_chunking_upstream(session)
    run = _matching_extraction_run(session, upstream=upstream)
    foreign_upstream = _seed_chunking_upstream(session, source_id=_SOURCE_B, output_fingerprint="other-fp")

    with pytest.raises(ValueError, match="does not resolve to a chunking or contextualization run"):
        record_extraction_run_applicability(
            session, run_id=run.id, upstream_run_id=foreign_upstream.id, input_policy="raw", resolved=_RESOLVED
        )


def test_accepts_a_contextualization_run_as_upstream(session: Session) -> None:
    """A contextualization (variant) run is a legal upstream, not only a chunking run."""
    variant_run = PipelineRun(
        stage=VARIANT_STAGE, scope_id=_SOURCE_A, status=RunStatus.ACTIVE, derivation_key="dk-variant"
    )
    session.add(variant_run)
    session.flush()
    session.add(
        ContextualizationRunInput(
            run_id=variant_run.id,
            summary_run_id=1,
            chunking_run_id=1,
            input_fingerprint="unused",
            config_fingerprint="unused",
            output_fingerprint="variant-output-fp",
        )
    )
    session.flush()
    key = extraction_derivation_key(
        input_fingerprint="variant-output-fp", config_fingerprint=_RESOLVED.config_fingerprint()
    )
    run = _seed_extraction_run(session, derivation_key=key)

    applicability = record_extraction_run_applicability(
        session, run_id=run.id, upstream_run_id=variant_run.id, input_policy="raw", resolved=_RESOLVED
    )

    assert applicability.input_fingerprint == "variant-output-fp"


# --------------------------------------------------------------------------- #
# Clause 3 — the candidate key recomputed from upstream output + resolved config matches
# --------------------------------------------------------------------------- #


def test_rejects_when_the_candidate_key_does_not_match_the_run(session: Session) -> None:
    """A run whose derivation key does not equal the candidate key computed from the upstream is rejected."""
    upstream = _seed_chunking_upstream(session)
    run = _seed_extraction_run(session, derivation_key="a-key-that-does-not-match-the-candidate")

    with pytest.raises(ValueError, match="does not match"):
        record_extraction_run_applicability(
            session, run_id=run.id, upstream_run_id=upstream.id, input_policy="raw", resolved=_RESOLVED
        )


# --------------------------------------------------------------------------- #
# Clause 4 — the run's ExtractionRunInput exists
# --------------------------------------------------------------------------- #


def test_rejects_when_the_run_has_no_extraction_run_input(session: Session) -> None:
    """A run whose ``ExtractionRunInput`` is missing (its persist is not complete) is rejected."""
    upstream = _seed_chunking_upstream(session)
    chunk_input = session.get(ChunkRunInput, upstream.id)
    key = extraction_derivation_key(
        input_fingerprint=chunk_input.chunk_set_fingerprint, config_fingerprint=_RESOLVED.config_fingerprint()
    )
    run = _seed_extraction_run(session, derivation_key=key, with_run_input=False)

    with pytest.raises(ValueError, match="no recorded ExtractionRunInput"):
        record_extraction_run_applicability(
            session, run_id=run.id, upstream_run_id=upstream.id, input_policy="raw", resolved=_RESOLVED
        )


# --------------------------------------------------------------------------- #
# Clause 6 — identical relation is a no-op; a conflicting one is rejected
# --------------------------------------------------------------------------- #


def test_an_identical_relation_is_a_no_op_returning_the_existing_row(session: Session) -> None:
    """Recording the same ``(run_id, upstream_run_id)`` relation twice returns the same row, not a duplicate."""
    upstream = _seed_chunking_upstream(session)
    run = _matching_extraction_run(session, upstream=upstream)

    first = record_extraction_run_applicability(
        session, run_id=run.id, upstream_run_id=upstream.id, input_policy="raw", resolved=_RESOLVED
    )
    session.flush()
    second = record_extraction_run_applicability(
        session, run_id=run.id, upstream_run_id=upstream.id, input_policy="raw", resolved=_RESOLVED
    )

    assert first.input_fingerprint == second.input_fingerprint
    rows = session.exec(
        select(ExtractionRunApplicability).where(
            ExtractionRunApplicability.run_id == run.id, ExtractionRunApplicability.upstream_run_id == upstream.id
        )
    ).all()
    assert len(rows) == 1, "the second call must not insert a duplicate row"


def test_a_conflicting_relation_at_the_same_primary_key_is_rejected(session: Session) -> None:
    """A different ``input_policy`` recorded at an already-present ``(run_id, upstream_run_id)`` has no update path."""
    upstream = _seed_chunking_upstream(session)
    run = _matching_extraction_run(session, upstream=upstream)
    record_extraction_run_applicability(
        session, run_id=run.id, upstream_run_id=upstream.id, input_policy="raw", resolved=_RESOLVED
    )
    session.flush()

    with pytest.raises(ValueError, match="no update path"):
        record_extraction_run_applicability(
            session, run_id=run.id, upstream_run_id=upstream.id, input_policy="contextualized", resolved=_RESOLVED
        )
