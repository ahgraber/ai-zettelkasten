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
:func:`~aizk.graph.chunks.document_order_chunks` and
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

from aizk.graph.chunks import CHUNKING_STAGE
from aizk.graph.contextualization import VARIANT_STAGE, StalePlanError
from aizk.graph.datamodel import (
    INPUT_KIND_CONTEXTUALIZED,
    INPUT_KIND_RAW,
    Chunk,
    ChunkRunInput,
    ChunkRunManifest,
    ContextualizationRunInput,
    ContextualizedChunk,
    ExtractionRunApplicability,
    ExtractionRunInput,
    Mention,
    MentionCooccurrence,
)
from aizk.graph.extraction import Detection
import aizk.graph.extraction_run as extraction_run_module
from aizk.graph.extraction_run import extract_corpus, extract_document, extract_source
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.graph.mentions import active_extraction_run
from aizk.pipeline.identity import content_fingerprint
from aizk.pipeline.run import PipelineRun, RunStatus, record_run

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_SCHEMA_TABLES = [
    Chunk.__table__,
    ChunkRunInput.__table__,
    ChunkRunManifest.__table__,
    ContextualizationRunInput.__table__,
    ContextualizedChunk.__table__,
    ExtractionRunApplicability.__table__,
    ExtractionRunInput.__table__,
    Mention.__table__,
    MentionCooccurrence.__table__,
    PipelineRun.__table__,
]

_SOURCE_A = UUID("11111111-1111-1111-1111-111111111111")
_SOURCE_B = UUID("22222222-2222-2222-2222-222222222222")


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
    source_id: UUID,
    texts: "Sequence[str]",
    derivation_key: str = "chunking-key-1",
) -> tuple[PipelineRun, list[Chunk]]:
    """Seed a chunking run, its Chunk rows, and its ``ChunkRunInput``, in document order.

    Bypasses ``persist_chunks`` (whose Markdown/content-index machinery this
    module does not exercise), building only what
    :func:`~aizk.graph.chunks.document_order_chunks` and
    ``extract_document``'s upstream-output revalidation need. Mirrors
    ``persist_chunks``'s surrogate-reuse rule: a chunk whose sameness-key
    ``(source_id, heading_path, ordinal, content_hash)`` is already present in
    this database reuses that row's ``chunk_id`` rather than minting a new one
    (so a re-chunk over unchanged content, on the same engine, is a legal
    call — two independent engines each still mint their own surrogate for the
    same logical content, as in the bulk-vs-incremental comparison below).
    ``chunk_set_fingerprint`` is computed the same way
    :func:`~aizk.graph.chunks.persist_chunks` would.
    """
    run = record_run(session, stage=CHUNKING_STAGE, scope_id=source_id, derivation_key=derivation_key)
    chunks: list[Chunk] = []
    cursor = 0
    for ordinal, text in enumerate(texts):
        content_hash = xxhash.xxh64(text.encode("utf-8")).hexdigest()
        chunk = session.exec(
            select(Chunk).where(
                Chunk.source_id == source_id,
                Chunk.heading_path_json == "[]",
                Chunk.ordinal == ordinal,
                Chunk.content_hash == content_hash,
            )
        ).one_or_none()
        if chunk is None:
            chunk = Chunk(
                chunk_id=uuid4(),
                content_hash=content_hash,
                source_id=source_id,
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
    session.add(
        ChunkRunInput(
            run_id=run.id,
            conversion_output_id=f"co-{run.id}",
            markdown_hash_xx64="deadbeefdeadbeef",
            chunk_set_fingerprint=payload_fingerprint(
                [PayloadItem(chunk_key(c), INPUT_KIND_RAW, content_fingerprint(c.text)) for c in chunks]
            ),
        )
    )
    session.commit()
    session.refresh(run)
    for chunk in chunks:
        session.refresh(chunk)
    return run, chunks


def _seed_variant_run(
    session: Session,
    *,
    source_id: UUID,
    derivation_key: str,
    chunking_run_id: int,
    chunks: "Sequence[Chunk]",
    revisions: "Mapping[UUID, str] | None" = None,
) -> PipelineRun:
    """Insert and commit an active :data:`VARIANT_STAGE` run with one variant row per chunk, and its run input.

    ``chunks`` is the chunking run's full ordered chunk set; ``revisions`` maps a
    chunk's ``chunk_id`` to its non-empty revision text — a chunk absent from
    ``revisions`` gets an empty-revision row (the self-contained case), matching
    how :func:`~aizk.graph.contextualization.contextualize_chunks` writes one
    variant per chunk in the chunking run's manifest. ``ContextualizationRunInput.output_fingerprint``
    is computed the same way, over exactly what
    :func:`~aizk.graph.extraction.select_extraction_input` would then read.
    """
    revisions = revisions or {}
    run = record_run(session, stage=VARIANT_STAGE, scope_id=source_id, derivation_key=derivation_key)
    session.flush()
    assert run.id is not None  # noqa: S101 -- flushed above
    items: list[PayloadItem] = []
    for chunk in chunks:
        revision = revisions.get(chunk.chunk_id, "")
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
        consumed_text = revision if revision != "" else chunk.text
        input_kind = INPUT_KIND_CONTEXTUALIZED if revision != "" else INPUT_KIND_RAW
        items.append(PayloadItem(chunk_key(chunk), input_kind, content_fingerprint(consumed_text)))
    session.add(
        ContextualizationRunInput(
            run_id=run.id,
            summary_run_id=1,
            chunking_run_id=chunking_run_id,
            input_fingerprint="test-input-fingerprint",
            config_fingerprint="test-config-fingerprint",
            output_fingerprint=payload_fingerprint(items),
        )
    )
    session.commit()
    session.refresh(run)
    return run


def _mention_signature(mention: Mention, ordinal_by_chunk_id: dict[str, int], source_id: UUID) -> tuple[object, ...]:
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


def _extraction_snapshot(engine: Engine, source_ids: "Sequence[UUID]") -> tuple[set[tuple], set[frozenset]]:
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
                c.chunk_id: c.ordinal for c in session.exec(select(Chunk).where(Chunk.source_id == source_id)).all()
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
        chunking_run_a, chunks_a = _seed_chunking_run(
            session,
            source_id=_SOURCE_A,
            texts=[
                "Acme Corp met Globex Inc today.",
                "The company announced strong results.",
            ],
            derivation_key="chunking-key-a",
        )
        assert chunking_run_a.id is not None
        _seed_variant_run(
            session,
            source_id=_SOURCE_A,
            derivation_key="variant-key-a",
            chunking_run_id=chunking_run_a.id,
            chunks=chunks_a,
            revisions={chunks_a[1].chunk_id: "Acme Corp announced strong results."},
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
        chunking_run, [chunk] = _seed_chunking_run(
            session, source_id=_SOURCE_A, texts=["Acme Corp announced results."]
        )
        chunk_id = chunk.chunk_id
        assert chunking_run.id is not None
        _seed_variant_run(
            session,
            source_id=_SOURCE_A,
            derivation_key="variant-key-1",
            chunking_run_id=chunking_run.id,
            chunks=[chunk],
            revisions={chunk_id: "Globex Inc, Acme Corp announced results."},
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


def test_extraction_run_input_resolves_the_chunking_run_under_raw_policy(tmp_path: Path) -> None:
    """Under ``raw`` policy, ``ExtractionRunInput`` names the chunking run and the raw-text payload fingerprint."""
    engine = _make_engine(tmp_path, "raw_run_input.db")
    chunk_text = "Acme Corp announced results."
    with Session(engine) as session:
        chunking_run, [chunk] = _seed_chunking_run(session, source_id=_SOURCE_A, texts=[chunk_text])
        # Capture primitives before any further commit on this session expires them.
        chunking_run_id = chunking_run.id
        expected_key = chunk_key(chunk)
        # An active variant exists, but input_policy="raw" must ignore it and read
        # the raw chunk, so the recorded upstream is the chunking run, not this one.
        assert chunking_run_id is not None
        _seed_variant_run(
            session,
            source_id=_SOURCE_A,
            derivation_key="variant-key-1",
            chunking_run_id=chunking_run_id,
            chunks=[chunk],
            revisions={chunk.chunk_id: "Globex Inc, Acme Corp."},
        )
    stub = _StubExtractor({chunk_text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]})

    result = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")

    with Session(engine) as session:
        run_input_row = session.get(ExtractionRunInput, result.run_id)
        assert run_input_row is not None
        assert run_input_row.input_policy == "raw"
        upstream = session.get(PipelineRun, run_input_row.upstream_run_id)
        assert upstream is not None
        assert upstream.stage == CHUNKING_STAGE
        assert run_input_row.upstream_run_id == chunking_run_id
        expected = payload_fingerprint([PayloadItem(expected_key, INPUT_KIND_RAW, content_fingerprint(chunk_text))])
        assert run_input_row.input_fingerprint == expected


def test_extraction_run_input_resolves_the_variant_run_under_contextualized_policy(tmp_path: Path) -> None:
    """Under ``contextualized`` policy with an active variant, ``ExtractionRunInput`` names the variant run."""
    engine = _make_engine(tmp_path, "contextualized_run_input.db")
    contextualized_text = "Globex Inc met with Acme Corp and announced results."
    span_start = contextualized_text.index("Acme Corp")
    span_end = span_start + len("Acme Corp")
    with Session(engine) as session:
        chunking_run, [chunk] = _seed_chunking_run(
            session, source_id=_SOURCE_A, texts=["Acme Corp announced results."]
        )
        expected_key = chunk_key(chunk)
        assert chunking_run.id is not None
        variant_run = _seed_variant_run(
            session,
            source_id=_SOURCE_A,
            derivation_key="variant-key-1",
            chunking_run_id=chunking_run.id,
            chunks=[chunk],
            revisions={chunk.chunk_id: contextualized_text},
        )
        variant_run_id = variant_run.id
    stub = _StubExtractor(
        {contextualized_text: [Detection(surface_form="Acme Corp", span_start=span_start, span_end=span_end)]}
    )

    result = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="contextualized")

    with Session(engine) as session:
        run_input_row = session.get(ExtractionRunInput, result.run_id)
        assert run_input_row is not None
        assert run_input_row.input_policy == "contextualized"
        assert run_input_row.upstream_run_id == variant_run_id
        upstream = session.get(PipelineRun, run_input_row.upstream_run_id)
        assert upstream is not None
        assert upstream.stage == VARIANT_STAGE
        expected = payload_fingerprint(
            [PayloadItem(expected_key, INPUT_KIND_CONTEXTUALIZED, content_fingerprint(contextualized_text))]
        )
        assert run_input_row.input_fingerprint == expected


# --------------------------------------------------------------------------- #
# Reconciliation and stale-plan revalidation
# --------------------------------------------------------------------------- #


def test_reconciliation_records_applicability_with_zero_extractor_calls_and_unchanged_run_input(
    tmp_path: Path,
) -> None:
    """A superseded-but-equivalent upstream reconciles: applicability is recorded, ExtractionRunInput untouched."""
    engine = _make_engine(tmp_path, "reconcile.db")
    chunk_text = "Acme Corp announced results."
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, texts=[chunk_text], derivation_key="chunking-v1")
    stub = _StubExtractor({chunk_text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]})

    first = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")
    with Session(engine) as session:
        original_run_input = session.get(ExtractionRunInput, first.run_id)
        assert original_run_input is not None
        original_upstream_run_id = original_run_input.upstream_run_id

    # Re-chunk over unchanged content: a new chunking run whose output
    # fingerprint equals the now-superseded one's, activated in its place.
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, texts=[chunk_text], derivation_key="chunking-v2")

    stub.calls.clear()
    second = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")

    assert stub.calls == [], "an equivalent upstream reconciles without invoking the extractor"
    assert second.run_id == first.run_id, "the extraction run itself is reused, not superseded"
    with Session(engine) as session:
        current_run_input = session.get(ExtractionRunInput, second.run_id)
        assert current_run_input is not None
        assert current_run_input.upstream_run_id == original_upstream_run_id, "the run-input row is never rewritten"
        new_chunking_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == CHUNKING_STAGE,
                PipelineRun.scope_id == _SOURCE_A,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        assert new_chunking_run.id != original_upstream_run_id
        applicability = session.get(ExtractionRunApplicability, (second.run_id, new_chunking_run.id))
        assert applicability is not None


def test_a_changed_upstream_output_between_read_and_write_raises_stale_plan_error(tmp_path: Path) -> None:
    """The upstream's output changing after the read phase raises a retryable StalePlanError; nothing is written."""
    engine = _make_engine(tmp_path, "stale_plan_output.db")
    chunk_text = "Acme Corp announced results."
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, texts=[chunk_text], derivation_key="chunking-v1")
    stub = _StubExtractor({chunk_text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]})

    real_replan = extraction_run_module._replan_upstream

    def _mutate_then_replan(session: Session, **kwargs: object) -> PipelineRun:
        # Simulate a re-chunk landing between the read and write phases: mutate
        # the active chunking run's persisted output fingerprint directly.
        chunking_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == CHUNKING_STAGE,
                PipelineRun.scope_id == _SOURCE_A,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one()
        chunk_input = session.get(ChunkRunInput, chunking_run.id)
        assert chunk_input is not None
        chunk_input.chunk_set_fingerprint = "mutated-fingerprint"
        session.add(chunk_input)
        return real_replan(session, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(extraction_run_module, "_replan_upstream", _mutate_then_replan)
        with pytest.raises(StalePlanError):
            extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")

    with Session(engine) as session:
        assert active_extraction_run(session, _SOURCE_A) is None, "no run was opened"
        assert session.exec(select(Mention)).all() == [], "no mentions were persisted"
        assert session.exec(select(ExtractionRunApplicability)).all() == [], "no applicability was recorded"


def test_the_active_run_superseded_between_read_and_write_raises_stale_plan_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reuse plan's active run no longer matching at persist raises StalePlanError; nothing is written.

    The candidate key is planned against the run the read phase observed;
    ``active_extraction_run`` is faked to report that same run for the read
    phase's own lookup (so the reuse branch is chosen, exactly as it would be
    without the fake) but to report no active run for the write phase's
    re-validation inside ``_reconcile_active_run`` — simulating a concurrent
    supersession landing in the window between the two, which the single-writer
    invariant does not expect but this revalidation guards against regardless.
    """
    engine = _make_engine(tmp_path, "stale_plan_superseded.db")
    chunk_text = "Acme Corp announced results."
    with Session(engine) as session:
        _seed_chunking_run(session, source_id=_SOURCE_A, texts=[chunk_text], derivation_key="chunking-v1")
    stub = _StubExtractor({chunk_text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]})
    first = extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")

    real_active_extraction_run = extraction_run_module.active_extraction_run
    calls = {"n": 0}

    def _fake_active_extraction_run(session: Session, source_id: UUID) -> PipelineRun | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_active_extraction_run(session, source_id)
        return None

    monkeypatch.setattr(extraction_run_module, "active_extraction_run", _fake_active_extraction_run)

    with pytest.raises(StalePlanError):
        extract_document(engine, source_id=_SOURCE_A, extractor=stub, input_policy="raw")

    monkeypatch.undo()
    with Session(engine) as session:
        active = active_extraction_run(session, _SOURCE_A)
        assert active is not None
        assert active.id == first.run_id, "nothing new was opened; the original run remains active"
        assert len(session.exec(select(Mention).where(Mention.run_id == first.run_id)).all()) == 1, (
            "the original run's mentions are untouched"
        )
        assert session.exec(select(ExtractionRunApplicability)).all() == [], "no applicability was recorded"
