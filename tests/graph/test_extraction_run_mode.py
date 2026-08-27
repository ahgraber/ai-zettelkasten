"""Behavioral tests for the run-mode entry points (``aizk.graph.extraction_run``).

Exercises :func:`~aizk.graph.extraction_run.extract_document` and its two thin
entry points, :func:`~aizk.graph.extraction_run.extract_source` (incremental) and
:func:`~aizk.graph.extraction_run.extract_corpus` (bulk/backfill): the
run-mode-independence contract (the same content extracted in each mode yields
the same mentions and co-occurrence links), the unit's own boundary contract
(an unknown ``input_policy`` and an unchunked source are both rejected clearly),
the ``raw`` policy's unconditional raw-text reading, and retry idempotency.

Builds Chunk rows and chunking/contextualization runs directly (bypassing
``persist_chunks``/``contextualize_chunks``, whose Markdown/LLM machinery this
module does not exercise) so each test only seeds what
:func:`~aizk.graph.persistence.document_order_chunks` and
:func:`~aizk.graph.extraction.select_extraction_input` read. A deterministic stub
:class:`~aizk.graph.extraction.EntityExtractor` keeps mentions reproducible,
mirroring ``tests/graph/test_extraction.py``'s conventions.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.graph.contextualization import VARIANT_STAGE
from aizk.graph.datamodel import Chunk, ChunkRunManifest, ContextualizedChunk, Mention, MentionCooccurrence
from aizk.graph.extraction import Detection
from aizk.graph.extraction_run import extract_corpus, extract_document, extract_source
from aizk.graph.mention_store import active_extraction_run
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.run import PipelineRun, RunStatus, record_run

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
    """Deterministic EntityExtractor test double returning configured detections per input text.

    Local to this module, mirroring ``tests/graph/test_extraction.py``'s
    StubExtractor: a fixed ``text -> detections`` mapping keyed by exact input
    text, so identical content extracted against independently-minted chunk
    surrogates in two different databases still drives identical detections.
    Also records every input text it is invoked on at :attr:`calls` (mirroring
    that module's RecordingExtractor), so a test can assert whether — and how
    often — the single extractor access point was reached.
    """

    def __init__(self, detections_by_text: dict[str, list[Detection]], *, extractor_version: str = "stub/v1") -> None:
        """Store the fixed ``text -> detections`` mapping and ``extractor_version``."""
        self._detections_by_text = detections_by_text
        self.extractor_version = extractor_version
        self.calls: list[str] = []

    def extract(self, text: str) -> "Sequence[Detection]":
        """Record ``text`` at :attr:`calls`, then return its configured detections (empty if unconfigured)."""
        self.calls.append(text)
        return list(self._detections_by_text.get(text, []))


def _make_engine(tmp_path: Path, name: str) -> Engine:
    """Create a file-based SQLite engine carrying only the tables this module touches."""
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=_SCHEMA_TABLES)
    return engine


def _seed_chunking_run(
    session: Session,
    *,
    source_id: str,
    texts: "Sequence[str]",
    derivation_key: str = "chunking-key-1",
) -> tuple[PipelineRun, list[Chunk]]:
    """Seed a chunking run and its Chunk rows (minted surrogate ids), in document order.

    Bypasses ``persist_chunks`` (which also records ``ChunkRunInput`` and indexes
    raw content, neither of which ``extract_document`` reads), building only what
    :func:`~aizk.graph.persistence.document_order_chunks` needs. ``chunk_id`` is
    minted per call, like ``persist_chunks`` mints a fresh surrogate per database
    for a novel sameness-key — so two separate calls for the same logical content
    (as in the bulk-vs-incremental comparison below) yield different chunk_ids.
    """
    run = record_run(session, stage=CHUNKING_STAGE, scope_id=source_id, derivation_key=derivation_key)
    chunks: list[Chunk] = []
    cursor = 0
    for ordinal, text in enumerate(texts):
        chunk = Chunk(
            chunk_id=str(uuid4()),
            content_hash=xxhash.xxh64(text.encode("utf-8")).hexdigest(),
            source_id=UUID(source_id),
            heading_path_json="[]",
            ordinal=ordinal,
            text=text,
            char_count=len(text),
        )
        session.add(chunk)
        session.add(
            ChunkRunManifest(run_id=run.id, chunk_id=chunk.chunk_id, span_start=cursor, span_end=cursor + len(text))
        )
        chunks.append(chunk)
        cursor += len(text) + 1
    session.commit()
    session.refresh(run)
    for chunk in chunks:
        session.refresh(chunk)
    return run, chunks


def _seed_variant_run(session: Session, *, source_id: str, derivation_key: str) -> PipelineRun:
    """Insert and commit an active :data:`VARIANT_STAGE` run for ``source_id``."""
    run = record_run(session, stage=VARIANT_STAGE, scope_id=source_id, derivation_key=derivation_key)
    session.commit()
    session.refresh(run)
    return run


def _seed_contextualized_chunk(
    session: Session,
    *,
    run_id: int,
    chunk_id: str,
    contextualized_text: str,
    context_version: int = 1,
) -> ContextualizedChunk:
    """Insert and commit a :class:`ContextualizedChunk` row under an already-seeded variant run."""
    row = ContextualizedChunk(
        run_id=run_id,
        summary_run_id=1,
        chunking_run_id=1,
        chunk_id=chunk_id,
        context_version=context_version,
        contextualized_text=contextualized_text,
        derivation_key="test-derivation-key",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _mention_signature(mention: Mention, ordinal_by_chunk_id: dict[str, int], source_id: str) -> tuple[object, ...]:
    """Return a mention's content-stable signature, normalized on chunk ordinal instead of raw chunk_id.

    Chunk surrogates are minted independently per database, so neither the raw
    ``chunk_id`` nor the stored ``source_occurrence_key`` (itself derived from
    that per-database ``chunk_id``) is directly comparable across databases. The
    document-order ``ordinal`` of the chunk stands in for chunk identity instead.
    """
    ordinal = ordinal_by_chunk_id[mention.chunk_id]
    if mention.anchor_kind == "source":
        return ("source", source_id, ordinal, mention.source_span_start, mention.source_span_end, mention.surface_form)
    return ("revision", source_id, ordinal, mention.surface_form)


def _extraction_snapshot(engine: Engine, source_ids: "Sequence[str]") -> tuple[set[tuple], set[frozenset]]:
    """Return (mention signatures, co-occurrence-pair signatures) for the sources' active extraction runs.

    Each pair signature is a ``frozenset`` of its two endpoints' mention
    signatures, so pairs compare by content rather than by the endpoints'
    per-database ``mention_id``s.
    """
    mention_sigs: set[tuple] = set()
    pair_sigs: set[frozenset] = set()
    with Session(engine) as session:
        for source_id in source_ids:
            run = active_extraction_run(session, source_id)
            assert run is not None, f"no active extraction run for {source_id!r}"
            ordinal_by_chunk_id = {
                c.chunk_id: c.ordinal
                for c in session.exec(select(Chunk).where(Chunk.source_id == UUID(source_id))).all()
            }
            mentions_by_id = {
                m.mention_id: m for m in session.exec(select(Mention).where(Mention.run_id == run.id)).all()
            }
            sig_by_mention_id = {
                mid: _mention_signature(m, ordinal_by_chunk_id, source_id) for mid, m in mentions_by_id.items()
            }
            mention_sigs.update(sig_by_mention_id.values())

            links = session.exec(select(MentionCooccurrence).where(MentionCooccurrence.run_id == run.id)).all()
            for link in links:
                pair_sigs.add(
                    frozenset({sig_by_mention_id[link.mention_id_lo], sig_by_mention_id[link.mention_id_hi]})
                )
    return mention_sigs, pair_sigs


def _build_corpus(engine: Engine) -> None:
    """Seed two sources' chunking (+ one contextualization) generations by content.

    Source A gets two chunks: one with two raw-occurring surfaces (a
    source-anchored co-occurring pair) and one whose active variant both names
    an entity absent from its raw text (a revision-anchored mention) and
    detects a surface present in the raw text (a source-anchored mention) — so
    that chunk yields a mixed-anchor co-occurrence pair with a revision
    endpoint. Source B gets one chunk with a single raw-occurring surface (a
    source-anchored singleton) and no contextualization run at all, exercising
    the no-contextualization-run upstream fallback.
    """
    with Session(engine) as session:
        _, chunks_a = _seed_chunking_run(
            session,
            source_id=_SOURCE_A,
            texts=[
                "Acme Corp met Globex Inc today.",
                "The company announced strong results.",
            ],
            derivation_key="chunking-key-a",
        )
        variant_run_a = _seed_variant_run(session, source_id=_SOURCE_A, derivation_key="variant-key-a")
        assert variant_run_a.id is not None
        _seed_contextualized_chunk(
            session,
            run_id=variant_run_a.id,
            chunk_id=chunks_a[1].chunk_id,
            contextualized_text="Acme Corp announced strong results.",
        )

        _seed_chunking_run(
            session,
            source_id=_SOURCE_B,
            texts=["Initech reported quarterly earnings."],
            derivation_key="chunking-key-b",
        )


_CORPUS_DETECTIONS = {
    "Acme Corp met Globex Inc today.": [
        Detection(surface_form="Acme Corp", span_start=0, span_end=9),
        Detection(surface_form="Globex Inc", span_start=14, span_end=24),
    ],
    "Acme Corp announced strong results.": [
        Detection(surface_form="Acme Corp", span_start=0, span_end=9),
        Detection(surface_form="results", span_start=27, span_end=34),
    ],
    "Initech reported quarterly earnings.": [
        Detection(surface_form="Initech", span_start=0, span_end=7),
    ],
}


# --------------------------------------------------------------------------- #
# EE6 — bulk / incremental partition
# --------------------------------------------------------------------------- #


def test_bulk_and_incremental_same_mentions(tmp_path: Path) -> None:
    """Bulk and incremental extraction of the same content yield the same mentions and co-occurrence links."""
    stub = _StubExtractor(_CORPUS_DETECTIONS, extractor_version="stub/v1")

    bulk_engine = _make_engine(tmp_path, "bulk.db")
    _build_corpus(bulk_engine)
    extract_corpus(bulk_engine, source_ids=[_SOURCE_A, _SOURCE_B], extractor=stub, input_policy="contextualized")

    incremental_engine = _make_engine(tmp_path, "incremental.db")
    _build_corpus(incremental_engine)
    extract_source(incremental_engine, source_id=_SOURCE_A, extractor=stub, input_policy="contextualized")
    extract_source(incremental_engine, source_id=_SOURCE_B, extractor=stub, input_policy="contextualized")

    bulk_mentions, bulk_pairs = _extraction_snapshot(bulk_engine, [_SOURCE_A, _SOURCE_B])
    incremental_mentions, incremental_pairs = _extraction_snapshot(incremental_engine, [_SOURCE_A, _SOURCE_B])

    bulk_source_sigs = {sig for sig in bulk_mentions if sig[0] == "source"}
    bulk_revision_sigs = {sig for sig in bulk_mentions if sig[0] == "revision"}
    # Guard against a vacuous equality (empty == empty).
    assert len(bulk_source_sigs) == 4
    assert len(bulk_revision_sigs) == 1
    assert len(bulk_pairs) == 2
    # The comparison must exercise a pair with a revision-anchored endpoint,
    # not only source-source pairs.
    assert any(any(sig[0] == "revision" for sig in pair) for pair in bulk_pairs)

    assert bulk_mentions == incremental_mentions
    assert bulk_pairs == incremental_pairs


# --------------------------------------------------------------------------- #
# extract_document's own boundary contract
# --------------------------------------------------------------------------- #


def test_extract_document_rejects_unknown_input_policy(tmp_path: Path) -> None:
    """An input_policy outside {contextualized, raw} is rejected fail-fast, before any extractor invocation."""
    engine = _make_engine(tmp_path, "unknown_policy.db")
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, texts=["Acme Corp announced results."])
    stub = _StubExtractor(
        {"Acme Corp announced results.": [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]}
    )

    with pytest.raises(ValueError, match="input_policy"):
        extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="bogus")  # type: ignore[arg-type]

    assert stub.calls == [], "the rejection must fire before any model work runs"


def test_extract_document_rejects_unchunked_source(tmp_path: Path) -> None:
    """A source with no active chunking run is rejected clearly: it has nothing to extract."""
    engine = _make_engine(tmp_path, "unchunked.db")
    stub = _StubExtractor({})

    with pytest.raises(ValueError, match="no active chunking run"):
        extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")


def test_raw_policy_records_raw_input_even_with_variant(tmp_path: Path) -> None:
    """Under input_policy=raw, extraction reads raw text unconditionally, even when a variant exists."""
    engine = _make_engine(tmp_path, "raw_policy.db")
    with Session(engine) as session:
        _, [chunk] = _seed_chunking_run(session, source_id=_SOURCE_A, texts=["Acme Corp announced results."])
        chunk_id = chunk.chunk_id
        variant_run = _seed_variant_run(session, source_id=_SOURCE_A, derivation_key="variant-key-1")
        assert variant_run.id is not None
        _seed_contextualized_chunk(
            session,
            run_id=variant_run.id,
            chunk_id=chunk_id,
            contextualized_text="Globex Inc, Acme Corp announced results.",
        )
    stub = _StubExtractor(
        {"Acme Corp announced results.": [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]}
    )

    result = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")

    assert result.mention_count == 1
    with Session(engine) as session:
        [mention] = session.exec(select(Mention).where(Mention.run_id == result.run_id)).all()
        assert mention.anchor_kind == "source"
        assert mention.input_kind == "raw"
        assert mention.input_ref == f'{{"chunk_id":"{chunk_id}"}}'


def test_retry_reuses_run_without_duplicating(tmp_path: Path) -> None:
    """Re-running extract_document with unchanged inputs reuses the active run and does not duplicate mentions."""
    engine = _make_engine(tmp_path, "retry.db")
    with Session(engine) as session:
        _seed_chunking_run(
            session,
            source_id=_SOURCE_A,
            texts=["Acme Corp met Globex Inc today."],
        )
    stub = _StubExtractor(
        {
            "Acme Corp met Globex Inc today.": [
                Detection(surface_form="Acme Corp", span_start=0, span_end=9),
                Detection(surface_form="Globex Inc", span_start=14, span_end=24),
            ]
        }
    )

    first = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")
    second = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")

    assert second.run_id == first.run_id
    assert second.mention_count == first.mention_count == 2
    with Session(engine) as session:
        assert len(session.exec(select(Mention).where(Mention.run_id == first.run_id)).all()) == 2
        assert (
            len(session.exec(select(MentionCooccurrence).where(MentionCooccurrence.run_id == first.run_id)).all()) == 1
        )
