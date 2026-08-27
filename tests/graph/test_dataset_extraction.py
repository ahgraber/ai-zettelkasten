"""Behavioral tests for the dataset run's orchestration (``aizk.graph.dataset_extraction``).

Covers :func:`~aizk.graph.dataset_extraction.resolve_target_source_ids` (explicit
vs. corpus-scan target selection, dedup, ordering, and ``limit``) and
:func:`~aizk.graph.dataset_extraction.run_dataset_extraction` (the confirmation
gate: an explicit target set runs unconfirmed, a corpus-scan selection refuses
until confirmed and writes nothing when refused, and a confirmed corpus scan
extracts every eligible source through the real
:func:`~aizk.graph.extraction_run.extract_corpus` path).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.graph.datamodel import Chunk, ChunkRunManifest, ContextualizedChunk, Mention, MentionCooccurrence
from aizk.graph.dataset_extraction import resolve_target_source_ids, run_dataset_extraction
from aizk.graph.extraction import Detection
from aizk.graph.mention_store import active_extraction_run
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.invalidation import ReprocessingConfirmationError
from aizk.pipeline.run import PipelineRun, record_run

if TYPE_CHECKING:
    from collections.abc import Sequence

_SCHEMA_TABLES = [
    Chunk.__table__,
    ChunkRunManifest.__table__,
    ContextualizedChunk.__table__,
    Mention.__table__,
    MentionCooccurrence.__table__,
    PipelineRun.__table__,
]

_SOURCE_A = "11111111-1111-1111-1111-111111111111"
_SOURCE_B = "22222222-2222-2222-2222-222222222222"


class _StubExtractor:
    """Deterministic EntityExtractor test double, mirroring ``tests/graph/test_extraction.py``'s StubExtractor."""

    def __init__(self, detections_by_text: dict[str, list[Detection]]) -> None:
        """Store the fixed ``text -> detections`` mapping."""
        self._detections_by_text = detections_by_text
        self.extractor_version = "stub/v1"

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return the configured detections for ``text`` (empty if unconfigured)."""
        return list(self._detections_by_text.get(text, []))


def _make_engine(tmp_path: Path, name: str) -> Engine:
    """Create a file-based SQLite engine carrying only the tables this module touches."""
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=_SCHEMA_TABLES)
    return engine


def _seed_chunking_run(session: Session, *, source_id: str, text: str, derivation_key: str) -> None:
    """Seed one chunk under a source's chunking run, the shape ``extract_document`` reads."""
    run = record_run(session, stage=CHUNKING_STAGE, scope_id=source_id, derivation_key=derivation_key)
    chunk_id = str(uuid4())
    session.add(
        Chunk(
            chunk_id=chunk_id,
            content_hash=xxhash.xxh64(text.encode("utf-8")).hexdigest(),
            source_id=UUID(source_id),
            heading_path_json="[]",
            ordinal=0,
            text=text,
            char_count=len(text),
        )
    )
    session.add(ChunkRunManifest(run_id=run.id, chunk_id=chunk_id, span_start=0, span_end=len(text)))
    session.commit()


# --------------------------------------------------------------------------- #
# resolve_target_source_ids
# --------------------------------------------------------------------------- #


def test_resolve_target_source_ids_explicit_dedupes_and_preserves_order(tmp_path: Path) -> None:
    """An explicit source_ids sequence is deduplicated but keeps first-seen order, ignoring limit."""
    engine = _make_engine(tmp_path, "explicit.db")
    with Session(engine) as session:
        targets = resolve_target_source_ids(session, source_ids=[_SOURCE_B, _SOURCE_A, _SOURCE_B], limit=1)
    assert targets == [_SOURCE_B, _SOURCE_A]


def test_resolve_target_source_ids_corpus_scan_orders_and_limits(tmp_path: Path) -> None:
    """With no explicit source_ids, every source with an active chunking run is targeted, capped by limit."""
    engine = _make_engine(tmp_path, "scan.db")
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, text="Acme Corp is a company.", derivation_key="dk-a")
        _seed_chunking_run(session, source_id=_SOURCE_B, text="Globex Inc is a company.", derivation_key="dk-b")

        unbounded = resolve_target_source_ids(session, source_ids=None, limit=None)
        assert set(unbounded) == {_SOURCE_A, _SOURCE_B}

        bounded = resolve_target_source_ids(session, source_ids=None, limit=1)
        assert bounded == unbounded[:1]


def test_resolve_target_source_ids_corpus_scan_excludes_unchunked_sources(tmp_path: Path) -> None:
    """A corpus scan targets only sources with an active chunking run; an unchunked source has nothing to extract."""
    engine = _make_engine(tmp_path, "unchunked.db")
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, text="Acme Corp is a company.", derivation_key="dk-a")

        targets = resolve_target_source_ids(session, source_ids=None, limit=None)
        assert targets == [_SOURCE_A]


# --------------------------------------------------------------------------- #
# run_dataset_extraction: the confirmation gate
# --------------------------------------------------------------------------- #


def test_run_dataset_extraction_explicit_sources_bypasses_confirmation(tmp_path: Path) -> None:
    """An explicit, operator-named target set runs unconfirmed and produces mentions."""
    engine = _make_engine(tmp_path, "explicit_run.db")
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, text="Acme Corp announced results.", derivation_key="dk-a")
    stub = _StubExtractor(
        {"Acme Corp announced results.": [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]}
    )

    results = run_dataset_extraction(
        engine, source_ids=[_SOURCE_A], limit=None, confirmed=False, extractor=stub, input_policy="raw"
    )

    assert [r.source_id for r in results] == [_SOURCE_A]
    assert results[0].mention_count == 1


def test_run_dataset_extraction_corpus_scan_requires_confirmation(tmp_path: Path) -> None:
    """A corpus-scan (no explicit source_ids) selection refuses to run until confirmed, writing nothing."""
    engine = _make_engine(tmp_path, "unconfirmed.db")
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, text="Acme Corp announced results.", derivation_key="dk-a")
    stub = _StubExtractor(
        {"Acme Corp announced results.": [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]}
    )

    with pytest.raises(ReprocessingConfirmationError, match="will not run until it is explicitly confirmed"):
        run_dataset_extraction(
            engine, source_ids=None, limit=None, confirmed=False, extractor=stub, input_policy="raw"
        )

    with Session(engine) as session:
        assert active_extraction_run(session, _SOURCE_A) is None, "nothing is extracted without confirmation"
        assert session.exec(select(Mention)).all() == []


def test_run_dataset_extraction_corpus_scan_confirmed_extracts_every_eligible_source(tmp_path: Path) -> None:
    """A confirmed corpus-scan selection extracts every source with an active chunking run."""
    engine = _make_engine(tmp_path, "confirmed.db")
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, text="Acme Corp announced results.", derivation_key="dk-a")
        _seed_chunking_run(session, source_id=_SOURCE_B, text="Globex Inc announced results.", derivation_key="dk-b")
    stub = _StubExtractor(
        {
            "Acme Corp announced results.": [Detection(surface_form="Acme Corp", span_start=0, span_end=9)],
            "Globex Inc announced results.": [Detection(surface_form="Globex Inc", span_start=0, span_end=10)],
        }
    )

    results = run_dataset_extraction(
        engine, source_ids=None, limit=None, confirmed=True, extractor=stub, input_policy="raw"
    )

    assert {r.source_id for r in results} == {_SOURCE_A, _SOURCE_B}
    with Session(engine) as session:
        assert active_extraction_run(session, _SOURCE_A) is not None
        assert active_extraction_run(session, _SOURCE_B) is not None
