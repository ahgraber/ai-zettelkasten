"""Tests for the extraction stage's currentness classification and re-admission.

A source's active extraction run is classified from persisted columns alone
(``aizk.graph.extraction_run.classify_extraction_sources`` /
``classify_extraction_source``): ``STALE`` when the payload or configuration a
re-extraction would consume differs from what the run recorded; ``NEEDS_RECONCILIATION``
when the payload is unchanged but the run recorded a different, now-superseded
upstream run than the one currently resolved; ``CURRENT`` otherwise. Staleness
(the ``STALE`` subset, ``stale_extraction_sources``) marks work an operator may
re-admit; it never makes a source pending.

Every case drives the real :func:`~aizk.graph.extraction_run.extract_document` over
a deterministic stub extractor, so the run records under test are the ones the write
path produces, not hand-built rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.graph.chunks import CHUNKING_STAGE
from aizk.graph.contextualization import VARIANT_STAGE
from aizk.graph.datamodel import (
    INPUT_KIND_CONTEXTUALIZED,
    INPUT_KIND_RAW,
    Chunk,
    ChunkRunInput,
    ChunkRunManifest,
    ContextualizationRunInput,
    ContextualizedChunk,
    ExtractionJob,
    ExtractionRunApplicability,
    ExtractionRunInput,
    Mention,
    MentionCooccurrence,
)
from aizk.graph.extraction_run import (
    Currentness,
    classify_extraction_source,
    classify_extraction_sources,
    extract_document,
    record_extraction_run_applicability,
    stale_extraction_sources,
)
from aizk.graph.extraction_workunit import pending_extraction_sources
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.graph.job_actions import apply_extraction_readmission
from aizk.graph.mentions import ResolvedExtractionConfig
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.identity import content_fingerprint
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, RunStatus, record_run

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aizk.graph.extraction import Detection

_SCHEMA_TABLES = [
    Chunk.__table__,
    ChunkRunInput.__table__,
    ChunkRunManifest.__table__,
    ContextualizationRunInput.__table__,
    ContextualizedChunk.__table__,
    ExtractionJob.__table__,
    ExtractionRunApplicability.__table__,
    ExtractionRunInput.__table__,
    Mention.__table__,
    MentionCooccurrence.__table__,
    PipelineEvent.__table__,
    PipelineRun.__table__,
]

_SOURCE_A = UUID("11111111-1111-1111-1111-111111111111")
_SOURCE_B = UUID("22222222-2222-2222-2222-222222222222")

_CHUNK_TEXT = "Ada Lovelace wrote the first algorithm."

#: The extraction configuration every test resolves against unless it says otherwise.
_RESOLVED_RAW = ResolvedExtractionConfig(extractor_version="stub/v1", materializer_version="1", input_policy="raw")
_RESOLVED_CONTEXTUALIZED = ResolvedExtractionConfig(
    extractor_version="stub/v1", materializer_version="1", input_policy="contextualized"
)


class _SilentExtractor:
    """An :class:`~aizk.graph.extraction.EntityExtractor` that detects nothing and records its calls.

    Currentness is decided by persisted columns, not by the mentions a run
    emits, so these tests need a deterministic extractor whose invocation count
    is directly observable — never a productive one.
    """

    extractor_version = "stub/v1"

    def __init__(self) -> None:
        """Initialize the empty call log."""
        self.calls: list[str] = []

    def extract(self, text: str) -> "Sequence[Detection]":
        """Record ``text`` and return no detections."""
        self.calls.append(text)
        return []


def _make_engine(tmp_path: Path, name: str = "staleness.db") -> Engine:
    """Create a file-based SQLite engine carrying only the tables these tests touch."""
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=_SCHEMA_TABLES)
    return engine


def _seed_chunking_run(
    engine: Engine, *, source_id: UUID, derivation_key: str, text: str = _CHUNK_TEXT
) -> tuple[int, Chunk]:
    """Activate a chunking run for a source with one chunk and its ``ChunkRunInput``; return (run_id, chunk).

    Mirrors ``persist_chunks``: the chunk is content-keyed, so a re-chunk over
    unchanged content reuses the existing row and only the manifest generation
    changes.
    """
    with Session(engine) as session:
        run = record_run(session, stage=CHUNKING_STAGE, scope_id=source_id, derivation_key=derivation_key)
        session.flush()
        content_hash = xxhash.xxh64(text.encode("utf-8")).hexdigest()
        chunk = session.exec(
            select(Chunk).where(Chunk.source_id == source_id, Chunk.content_hash == content_hash)
        ).one_or_none()
        if chunk is None:
            chunk = Chunk(
                chunk_id=uuid4(),
                content_hash=content_hash,
                source_id=source_id,
                heading_path_json="[]",
                ordinal=0,
                text=text,
                char_count=len(text),
            )
            session.add(chunk)
        session.add(ChunkRunManifest(run_id=run.id, chunk_id=chunk.chunk_id, span_start=0, span_end=len(text)))
        session.add(
            ChunkRunInput(
                run_id=run.id,
                conversion_output_id=f"co-{run.id}",
                markdown_hash_xx64="deadbeefdeadbeef",
                chunk_set_fingerprint=payload_fingerprint(
                    [PayloadItem(chunk_key(chunk), INPUT_KIND_RAW, content_fingerprint(chunk.text))]
                ),
            )
        )
        session.commit()
        session.refresh(chunk)
        return run.id, chunk


def _seed_variant_run(
    engine: Engine, *, source_id: UUID, derivation_key: str, chunking_run_id: int, chunk: Chunk, revision: str
) -> int:
    """Activate a contextualization run for a source with one complete variant; return its run id."""
    with Session(engine) as session:
        run = record_run(session, stage=VARIANT_STAGE, scope_id=source_id, derivation_key=derivation_key)
        session.flush()
        session.add(
            ContextualizedChunk(
                run_id=run.id,
                summary_run_id=1,
                chunking_run_id=chunking_run_id,
                chunk_id=chunk.chunk_id,
                context_version=1,
                contextualized_text=revision,
                derivation_key="test-derivation-key",
            )
        )
        consumed_kind = INPUT_KIND_CONTEXTUALIZED if revision != "" else INPUT_KIND_RAW
        consumed_text = revision if revision != "" else chunk.text
        session.add(
            ContextualizationRunInput(
                run_id=run.id,
                summary_run_id=1,
                chunking_run_id=chunking_run_id,
                input_fingerprint="test-input-fingerprint",
                config_fingerprint="test-config-fingerprint",
                output_fingerprint=payload_fingerprint(
                    [PayloadItem(chunk_key(chunk), consumed_kind, content_fingerprint(consumed_text))]
                ),
            )
        )
        session.commit()
        return run.id


def _extract(engine: Engine, source_id: UUID, *, input_policy: str = "raw") -> int:
    """Run the write path for one source and return its active extraction run id."""
    return extract_document(
        engine, source_id=source_id, extractor=_SilentExtractor(), input_policy=input_policy
    ).run_id


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def test_a_re_chunked_source_with_changed_content_is_stale(tmp_path: Path) -> None:
    """A chunking generation with different content leaves the extracted run's recorded payload behind."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)

    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2", text="Different content entirely.")

    with Session(engine) as session:
        assert classify_extraction_sources(session, resolved=_RESOLVED_RAW) == {_SOURCE_A: Currentness.STALE}
        assert stale_extraction_sources(session, resolved=_RESOLVED_RAW) == {_SOURCE_A}


def test_a_newly_contextualized_source_is_stale(tmp_path: Path) -> None:
    """A complete variant appearing where extraction previously fell back to raw leaves it behind."""
    engine = _make_engine(tmp_path)
    chunking_run_id, chunk = _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A, input_policy="contextualized")

    _seed_variant_run(
        engine,
        source_id=_SOURCE_A,
        derivation_key="variant-v1",
        chunking_run_id=chunking_run_id,
        chunk=chunk,
        revision="Ada Lovelace, an English mathematician, wrote the first algorithm.",
    )

    with Session(engine) as session:
        assert classify_extraction_sources(session, resolved=_RESOLVED_CONTEXTUALIZED) == {
            _SOURCE_A: Currentness.STALE
        }


def test_a_current_source_is_current(tmp_path: Path) -> None:
    """A source whose extraction consumed its current active upstream state is current."""
    engine = _make_engine(tmp_path)
    chunking_run_id, chunk = _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _seed_variant_run(
        engine,
        source_id=_SOURCE_A,
        derivation_key="variant-v1",
        chunking_run_id=chunking_run_id,
        chunk=chunk,
        revision="Ada Lovelace, an English mathematician, wrote the first algorithm.",
    )
    _extract(engine, _SOURCE_A, input_policy="contextualized")

    with Session(engine) as session:
        assert classify_extraction_sources(session, resolved=_RESOLVED_CONTEXTUALIZED) == {
            _SOURCE_A: Currentness.CURRENT
        }
        assert stale_extraction_sources(session, resolved=_RESOLVED_CONTEXTUALIZED) == set()


def test_an_equivalent_superseded_upstream_needs_reconciliation_and_reconciles_on_extraction(
    tmp_path: Path,
) -> None:
    """A re-chunk over unchanged content needs reconciliation; re-extracting then reconciles with zero calls."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    with Session(engine) as session:
        original_run_input = session.exec(
            select(ExtractionRunInput)
            .join(PipelineRun, PipelineRun.id == ExtractionRunInput.run_id)
            .where(PipelineRun.scope_id == _SOURCE_A, PipelineRun.status == RunStatus.ACTIVE)
        ).one()
        original_upstream_run_id = original_run_input.upstream_run_id
        original_run_id = original_run_input.run_id

    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        assert classify_extraction_sources(session, resolved=_RESOLVED_RAW) == {
            _SOURCE_A: Currentness.NEEDS_RECONCILIATION
        }
        assert stale_extraction_sources(session, resolved=_RESOLVED_RAW) == set()

    stub = _SilentExtractor()
    reconciled_run_id = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw").run_id

    assert stub.calls == [], "reconciliation never invokes the extractor"
    assert reconciled_run_id == original_run_id, "the extraction run itself is reused"
    with Session(engine) as session:
        current_run_input = session.get(ExtractionRunInput, reconciled_run_id)
        assert current_run_input.upstream_run_id == original_upstream_run_id, "the run-input row is never rewritten"
        new_chunking_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == CHUNKING_STAGE,
                PipelineRun.scope_id == _SOURCE_A,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        assert session.get(ExtractionRunApplicability, (reconciled_run_id, new_chunking_run.id)) is not None
        assert classify_extraction_source(session, _SOURCE_A, resolved=_RESOLVED_RAW) is Currentness.CURRENT


def test_unchanged_payload_with_a_changed_resolved_extractor_version_is_stale(tmp_path: Path) -> None:
    """A configuration change alone (unchanged payload) is stale under the newly-resolved configuration."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)

    changed = ResolvedExtractionConfig(extractor_version="stub/v2", materializer_version="1", input_policy="raw")

    with Session(engine) as session:
        assert classify_extraction_sources(session, resolved=changed) == {_SOURCE_A: Currentness.STALE}
        # The unchanged configuration still reads current — the verdict is about
        # the resolved configuration supplied, not an absolute property.
        assert classify_extraction_sources(session, resolved=_RESOLVED_RAW) == {_SOURCE_A: Currentness.CURRENT}


def test_an_equivalent_reconciled_upstream_is_current_not_stale(tmp_path: Path) -> None:
    """Once applicability is recorded for a superseded-but-equivalent upstream, the source reads current."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    run_id = _extract(engine, _SOURCE_A)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        new_chunking_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == CHUNKING_STAGE,
                PipelineRun.scope_id == _SOURCE_A,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        record_extraction_run_applicability(
            session,
            run_id=run_id,
            upstream_run_id=new_chunking_run.id,
            input_policy="raw",
            resolved=_RESOLVED_RAW,
        )
        session.commit()

    with Session(engine) as session:
        assert classify_extraction_source(session, _SOURCE_A, resolved=_RESOLVED_RAW) is Currentness.CURRENT
        assert stale_extraction_sources(session, resolved=_RESOLVED_RAW) == set()


def test_a_source_with_no_extraction_run_is_not_classified(tmp_path: Path) -> None:
    """Currentness describes work already done; an unextracted source is absent from the classification."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")

    with Session(engine) as session:
        assert classify_extraction_sources(session, resolved=_RESOLVED_RAW) == {}
        assert classify_extraction_source(session, _SOURCE_A, resolved=_RESOLVED_RAW) is None
        assert stale_extraction_sources(session, resolved=_RESOLVED_RAW) == set()


def test_staleness_is_per_source(tmp_path: Path) -> None:
    """A superseded generation for one source does not mark another source stale."""
    engine = _make_engine(tmp_path)
    for source_id in (_SOURCE_A, _SOURCE_B):
        _seed_chunking_run(engine, source_id=source_id, derivation_key="chunking-v1")
        _extract(engine, source_id)

    _seed_chunking_run(engine, source_id=_SOURCE_B, derivation_key="chunking-v2", text="Something else entirely.")

    with Session(engine) as session:
        assert stale_extraction_sources(session, resolved=_RESOLVED_RAW) == {_SOURCE_B}


def test_the_single_source_check_agrees_with_the_corpus_classification(tmp_path: Path) -> None:
    """The per-unit check and the corpus scan classify a source identically — both share one code path."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2", text="Something else entirely.")

    with Session(engine) as session:
        corpus = classify_extraction_sources(session, resolved=_RESOLVED_RAW)
        assert classify_extraction_source(session, _SOURCE_A, resolved=_RESOLVED_RAW) == corpus.get(_SOURCE_A)
        assert corpus[_SOURCE_A] is Currentness.STALE


def test_corpus_classification_statement_count_does_not_grow_with_corpus_size_and_loads_no_chunk_text(
    tmp_path: Path,
) -> None:
    """The corpus classification issues a fixed number of statements and never selects chunk/variant text."""
    small_engine = _make_engine(tmp_path, "small.db")
    _seed_chunking_run(small_engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(small_engine, _SOURCE_A)

    large_engine = _make_engine(tmp_path, "large.db")
    for index in range(5):
        source_id = UUID(f"{index + 1:08d}-0000-0000-0000-000000000000")
        _seed_chunking_run(large_engine, source_id=source_id, derivation_key="chunking-v1")
        _extract(large_engine, source_id)

    def _measure(engine: Engine) -> tuple[dict, list[str]]:
        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
            statements.append(statement)

        event.listen(engine, "after_cursor_execute", _record)
        try:
            with Session(engine) as session:
                result = classify_extraction_sources(session, resolved=_RESOLVED_RAW)
        finally:
            event.remove(engine, "after_cursor_execute", _record)
        return result, statements

    small_result, small_statements = _measure(small_engine)
    large_result, large_statements = _measure(large_engine)

    assert len(small_result) == 1
    assert len(large_result) == 5
    assert len(small_statements) == len(large_statements)
    for statement in small_statements + large_statements:
        assert "graph_chunks.text" not in statement
        assert "contextualized_text" not in statement


# --------------------------------------------------------------------------- #
# Re-admission
# --------------------------------------------------------------------------- #


def _seed_unit(engine: Engine, source_id: UUID, status: WorkUnitStatus) -> int:
    """Insert the source's extraction work-unit in a given status; return its id."""
    with Session(engine) as session:
        job = ExtractionJob(idempotency_key=f"source:{source_id}", source_id=source_id, status=status)
        session.add(job)
        session.commit()
        return job.id


def test_re_admission_requeues_a_stale_finished_unit_and_fresh_extraction_supersedes(tmp_path: Path) -> None:
    """Re-extracting a stale source queues its unit, keeping its attempt count; the next execution supersedes the prior run."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    first_run_id = _extract(engine, _SOURCE_A)
    job_id = _seed_unit(engine, _SOURCE_A, WorkUnitStatus.SUCCEEDED)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2", text="Something else entirely.")

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        job.attempts = 3
        apply_extraction_readmission(session, job, resolved=_RESOLVED_RAW, principal="operator")
        session.commit()

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        assert job.status is WorkUnitStatus.QUEUED
        assert job.attempts == 3
        events = session.exec(select(PipelineEvent).where(PipelineEvent.work_unit_ref == str(job_id))).all()
        assert [event.kind for event in events] == ["requeued"]

    second_run_id = _extract(engine, _SOURCE_A)

    assert second_run_id != first_run_id, "the fresh extraction supersedes the prior run"
    with Session(engine) as session:
        assert stale_extraction_sources(session, resolved=_RESOLVED_RAW) == set()


def test_re_admission_requeues_a_needs_reconciliation_unit_and_processing_records_applicability(
    tmp_path: Path,
) -> None:
    """Re-extracting a needs-reconciliation source queues its unit; the next execution reconciles, not re-extracts."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    first_run_id = _extract(engine, _SOURCE_A)
    job_id = _seed_unit(engine, _SOURCE_A, WorkUnitStatus.SUCCEEDED)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2")

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        apply_extraction_readmission(session, job, resolved=_RESOLVED_RAW, principal="operator")
        session.commit()

    with Session(engine) as session:
        assert session.get(ExtractionJob, job_id).status is WorkUnitStatus.QUEUED

    stub = _SilentExtractor()
    second_run_id = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw").run_id

    assert stub.calls == [], "reconciliation never invokes the extractor"
    assert second_run_id == first_run_id, "the run is retained, not superseded"
    with Session(engine) as session:
        assert classify_extraction_source(session, _SOURCE_A, resolved=_RESOLVED_RAW) is Currentness.CURRENT


def test_re_admission_skips_a_unit_whose_source_is_current(tmp_path: Path) -> None:
    """A finished unit on a current source is ineligible and left exactly as it was."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    job_id = _seed_unit(engine, _SOURCE_A, WorkUnitStatus.SUCCEEDED)

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        try:
            apply_extraction_readmission(session, job, resolved=_RESOLVED_RAW, principal="operator")
        except ValueError as exc:
            assert "current" in str(exc)
        else:
            raise AssertionError("expected a ValueError for a current source")
        session.rollback()

    with Session(engine) as session:
        assert session.get(ExtractionJob, job_id).status is WorkUnitStatus.SUCCEEDED


def test_re_admission_skips_an_unfinished_unit(tmp_path: Path) -> None:
    """A unit that has not finished is already going to run, so re-extraction is ineligible."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    job_id = _seed_unit(engine, _SOURCE_A, WorkUnitStatus.QUEUED)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2", text="Something else entirely.")

    with Session(engine) as session:
        job = session.get(ExtractionJob, job_id)
        try:
            apply_extraction_readmission(session, job, resolved=_RESOLVED_RAW, principal="operator")
        except ValueError as exc:
            assert "status" in str(exc)
        else:
            raise AssertionError("expected a ValueError for an unfinished unit")
        session.rollback()

    with Session(engine) as session:
        assert session.get(ExtractionJob, job_id).status is WorkUnitStatus.QUEUED


def test_a_stale_source_is_not_pending(tmp_path: Path) -> None:
    """Staleness never admits work: a stale source stays out of the pending set."""
    engine = _make_engine(tmp_path)
    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v1")
    _extract(engine, _SOURCE_A)
    with Session(engine) as session:
        session.add(
            ExtractionJob(
                idempotency_key=f"source:{_SOURCE_A}",
                source_id=_SOURCE_A,
                status=WorkUnitStatus.SUCCEEDED,
            )
        )
        session.commit()

    _seed_chunking_run(engine, source_id=_SOURCE_A, derivation_key="chunking-v2", text="Something else entirely.")

    with Session(engine) as session:
        assert stale_extraction_sources(session, resolved=_RESOLVED_RAW) == {_SOURCE_A}
        assert pending_extraction_sources(session) == []
