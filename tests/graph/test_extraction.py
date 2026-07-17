"""Behavioral tests for the extraction pipeline (``aizk.graph.extraction``).

Exercises :func:`~aizk.graph.extraction.select_extraction_input` and
:func:`~aizk.graph.extraction.extract_chunk_mentions` against a real (file-based)
SQLite schema carrying the chunk, contextualized-chunk, mention, co-occurrence,
and shared ``pipeline_runs`` tables — the same tables
``tests/graph/test_mention_store.py`` builds, plus the write path
(:func:`~aizk.graph.mention_store.open_extraction_run` /
:func:`~aizk.graph.mention_store.persist_mentions`) they feed. Extraction is
wired end-to-end through a deterministic stub :class:`~aizk.graph.extraction.EntityExtractor`
(design decision ``DeterministicStubExtractorForTests``): uniform run versions,
the source/revision/expansion occurrence-classification partition, intra-chunk
co-occurrence, raw-vs-contextualized input selection (including the
present-empty case the store rejects if misrecorded), and extractor
substitutability through the single access point.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.graph.contextualization import VARIANT_STAGE
from aizk.graph.datamodel import Chunk, ContextualizedChunk, Mention, MentionCooccurrence
from aizk.graph.extraction import (
    MATERIALIZER_VERSION,
    Detection,
    EntityExtractor,
    ExtractionInput,
    extract_chunk_mentions,
    select_extraction_input,
)
from aizk.graph.mention_store import open_extraction_run, persist_mentions
from aizk.pipeline.run import PipelineRun, RunStatus, record_run

if TYPE_CHECKING:
    from collections.abc import Sequence

import pytest

_SCHEMA_TABLES = [
    Chunk.__table__,
    ContextualizedChunk.__table__,
    Mention.__table__,
    MentionCooccurrence.__table__,
    PipelineRun.__table__,
]

_SOURCE_A = "11111111-1111-1111-1111-111111111111"
_SOURCE_B = "22222222-2222-2222-2222-222222222222"


# --------------------------------------------------------------------------- #
# Stub extractors (design decision DeterministicStubExtractorForTests)
# --------------------------------------------------------------------------- #


class StubExtractor:
    """Deterministic :class:`EntityExtractor` test double returning configured detections per input text.

    Mirrors :class:`aizk.graph.llm.StubLLMClient`: a fixed ``text -> detections``
    mapping and a fixed ``extractor_version``, so a test exercises extraction's
    own materialization logic (grouping, occurrence classification) without
    depending on a real NER model.
    """

    def __init__(self, detections_by_text: dict[str, list[Detection]], *, extractor_version: str = "stub/v1") -> None:
        """Store the fixed ``text -> detections`` mapping and ``extractor_version``."""
        self._detections_by_text = detections_by_text
        self.extractor_version = extractor_version

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return the configured detections for ``text`` (empty if unconfigured)."""
        return list(self._detections_by_text.get(text, []))


class RecordingExtractor:
    """Wraps another :class:`EntityExtractor`, recording every ``extract()`` call it receives.

    Used to verify extraction makes every NER call through the single access
    point and none outside it.
    """

    def __init__(self, inner: EntityExtractor) -> None:
        """Wrap ``inner``, adopting its ``extractor_version`` and recording calls at :attr:`calls`."""
        self._inner = inner
        self.extractor_version = inner.extractor_version
        self.calls: list[str] = []

    def extract(self, text: str) -> "Sequence[Detection]":
        """Record ``text`` at :attr:`calls`, then delegate to the wrapped extractor."""
        self.calls.append(text)
        return self._inner.extract(text)


# --------------------------------------------------------------------------- #
# Fixtures (mirrors tests/graph/test_mention_store.py's engine/chunk seeding)
# --------------------------------------------------------------------------- #


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """Return a file-based SQLite URL for a per-test database."""
    return f"sqlite:///{tmp_path / 'extraction.db'}"


@pytest.fixture
def engine(db_url: str) -> Engine:
    """A SQLite engine carrying only the chunk/mention/run tables this module touches."""
    eng = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng, tables=_SCHEMA_TABLES)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Session:
    """An open session on the schema engine; the test owns commit boundaries."""
    with Session(engine) as sess:
        yield sess


def _seed_chunk(
    session: Session,
    *,
    chunk_id: str,
    text: str,
    source_id: str = _SOURCE_A,
    ordinal: int = 0,
    heading_path_json: str = "[]",
) -> Chunk:
    """Insert and commit a :class:`Chunk` row with the given surrogate id and text."""
    row = Chunk(
        chunk_id=chunk_id,
        content_hash=xxhash.xxh64(text.encode("utf-8")).hexdigest(),
        source_id=source_id,
        heading_path_json=heading_path_json,
        ordinal=ordinal,
        text=text,
        char_count=len(text),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _seed_variant_run(session: Session, *, source_id: str, derivation_key: str) -> PipelineRun:
    """Insert and commit an active :data:`VARIANT_STAGE` run for ``source_id``, superseding any prior one."""
    run = record_run(session, stage=VARIANT_STAGE, scope_id=source_id, derivation_key=derivation_key)
    session.commit()
    session.refresh(run)
    return run


def _seed_contextualized_chunk(
    session: Session,
    *,
    chunk_id: str,
    run_id: int,
    contextualized_text: str,
    context_version: int = 1,
    summary_run_id: int = 1,
    chunking_run_id: int = 1,
) -> ContextualizedChunk:
    """Insert and commit a :class:`ContextualizedChunk` row under an already-seeded variant run."""
    row = ContextualizedChunk(
        run_id=run_id,
        summary_run_id=summary_run_id,
        chunking_run_id=chunking_run_id,
        chunk_id=chunk_id,
        context_version=context_version,
        contextualized_text=contextualized_text,
        derivation_key="test-derivation-key",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _extract_chunk(
    session: Session,
    *,
    chunk: Chunk,
    extractor: EntityExtractor,
    upstream_derivation_key: str = "chunking-key-1",
) -> tuple[PipelineRun, list[Mention]]:
    """Extract and persist one chunk's mentions, mirroring the stage's write path end-to-end.

    Resolves the chunk's input, extracts drafts, opens (or reuses) the source's
    extraction run, and persists — committing, since none of the underlying
    calls do.
    """
    extraction_input = select_extraction_input(session, chunk)
    drafts = extract_chunk_mentions(
        chunk_id=chunk.chunk_id,
        raw_chunk_text=chunk.text,
        extraction_input=extraction_input,
        extractor=extractor,
    )
    run = open_extraction_run(
        session,
        source_id=chunk.source_id,
        extractor_version=extractor.extractor_version,
        materializer_version=MATERIALIZER_VERSION,
        input_policy="contextualized",
        upstream_derivation_key=upstream_derivation_key,
    )
    mentions = persist_mentions(session, run=run, mentions=drafts)
    session.commit()
    session.refresh(run)
    return run, mentions


def _cooccurring_ids(session: Session, run_id: int, mention_id: str) -> set[str]:
    """Return the mention ids co-occurring with ``mention_id`` in ``run_id``, resolved off the link table."""
    lo_side = session.exec(
        select(MentionCooccurrence.mention_id_hi).where(
            MentionCooccurrence.run_id == run_id, MentionCooccurrence.mention_id_lo == mention_id
        )
    ).all()
    hi_side = session.exec(
        select(MentionCooccurrence.mention_id_lo).where(
            MentionCooccurrence.run_id == run_id, MentionCooccurrence.mention_id_hi == mention_id
        )
    ).all()
    return set(lo_side) | set(hi_side)


# --------------------------------------------------------------------------- #
# EE1 — mentions persisted under the run; uniform run versions
# --------------------------------------------------------------------------- #


def test_mentions_under_run(session: Session) -> None:
    """Every identified mention persists as a record belonging to the run and sourced to the chunk."""
    chunk = _seed_chunk(session, chunk_id="c1", text="Acme Corp met Globex Inc today.")
    stub = StubExtractor(
        {
            chunk.text: [
                Detection(surface_form="Acme Corp", span_start=0, span_end=9),
                Detection(surface_form="Globex Inc", span_start=14, span_end=24),
            ]
        }
    )

    run, mentions = _extract_chunk(session, chunk=chunk, extractor=stub)

    assert len(mentions) == 2
    assert all(m.run_id == run.id for m in mentions)
    assert all(m.chunk_id == chunk.chunk_id for m in mentions)
    assert {m.surface_form for m in mentions} == {"Acme Corp", "Globex Inc"}


def test_uniform_run_versions(session: Session) -> None:
    """Mentions emitted from multiple chunks in one run all equal the run's recorded versions."""
    chunk1 = _seed_chunk(session, chunk_id="c1", text="Acme Corp is here.")
    chunk2 = _seed_chunk(session, chunk_id="c2", text="Globex Inc is here.", ordinal=1)
    stub = StubExtractor(
        {
            chunk1.text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)],
            chunk2.text: [Detection(surface_form="Globex Inc", span_start=0, span_end=10)],
        }
    )

    _, mentions1 = _extract_chunk(session, chunk=chunk1, extractor=stub)
    run2, mentions2 = _extract_chunk(session, chunk=chunk2, extractor=stub)

    # Unchanged derivation key: both chunks landed in the same active run.
    all_mentions = mentions1 + mentions2
    assert {m.run_id for m in all_mentions} == {run2.id}
    stamps = json.loads(run2.version_stamps_json)
    assert stamps["extractor_version"] == stub.extractor_version
    assert stamps["materializer_version"] == MATERIALIZER_VERSION


# --------------------------------------------------------------------------- #
# EE2 — source / revision / expansion partition
# --------------------------------------------------------------------------- #


def test_source_two_spans_coincide(session: Session) -> None:
    """A once-occurring surface form's input_span and source_chunk_span both resolve to it in the raw chunk."""
    chunk = _seed_chunk(session, chunk_id="c1", text="Acme Corp announced results.")
    stub = StubExtractor({chunk.text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]})

    _, [mention] = _extract_chunk(session, chunk=chunk, extractor=stub)

    assert mention.anchor_kind == "source"
    assert chunk.text[mention.input_span_start : mention.input_span_end] == mention.surface_form
    assert chunk.text[mention.source_span_start : mention.source_span_end] == mention.surface_form
    assert (mention.input_span_start, mention.input_span_end) == (mention.source_span_start, mention.source_span_end)


def test_revision_only_name_emitted_as_revision_anchored(session: Session) -> None:
    """A revision-resolved name absent from the raw chunk is emitted as one revision-anchored mention."""
    chunk = _seed_chunk(session, chunk_id="c1", text="The company announced results today.")
    variant_run = _seed_variant_run(session, source_id=chunk.source_id, derivation_key="variant-key-1")
    variant = _seed_contextualized_chunk(
        session,
        chunk_id=chunk.chunk_id,
        run_id=variant_run.id,
        contextualized_text="Acme Corp announced results today.",
    )
    stub = StubExtractor(
        {variant.contextualized_text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]}
    )

    _, [mention] = _extract_chunk(session, chunk=chunk, extractor=stub)

    assert mention.anchor_kind == "revision"
    assert mention.source_span_start is None
    assert mention.source_span_end is None
    assert mention.input_kind == "contextualized"


def test_repeated_surface_expands_per_occurrence(session: Session) -> None:
    """A surface form occurring three times in the raw chunk yields three source-anchored mentions."""
    chunk_text = "Acme Corp met Acme Corp's rival, who then called Acme Corp again."
    chunk = _seed_chunk(session, chunk_id="c1", text=chunk_text)
    stub = StubExtractor({chunk_text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]})

    _, mentions = _extract_chunk(session, chunk=chunk, extractor=stub)

    assert len(mentions) == 3
    assert all(m.anchor_kind == "source" for m in mentions)
    spans = {(m.source_span_start, m.source_span_end) for m in mentions}
    assert len(spans) == 3
    for start, end in spans:
        assert chunk_text[start:end] == "Acme Corp"


@pytest.mark.parametrize(
    ("raw_text", "near_miss"),
    [
        ("the acme corp announced results today.", "acme corp"),
        ("Acme  Corp announced results today.", "Acme  Corp"),
    ],
    ids=["case-differing", "whitespace-differing"],
)
def test_near_miss_surface_is_revision_anchored_not_fuzzily_matched(
    session: Session, raw_text: str, near_miss: str
) -> None:
    """A surface absent verbatim from the raw chunk is revision-anchored even when a near-miss form is present.

    The variant states the entity verbatim (``"Acme Corp"``) while the raw chunk
    carries only a case- or whitespace-differing form. Classification is exact
    string match against the raw text (:func:`_raw_occurrences`), so it finds
    zero occurrences of the detected surface and emits one revision-anchored
    mention with no source span — never assigning the detection to the near-miss
    raw span positionally or by fuzzy matching, the rule the source/revision
    split forbids.
    """
    chunk = _seed_chunk(session, chunk_id="c1", text=raw_text)
    variant_run = _seed_variant_run(session, source_id=chunk.source_id, derivation_key="variant-key-1")
    variant = _seed_contextualized_chunk(
        session,
        chunk_id=chunk.chunk_id,
        run_id=variant_run.id,
        contextualized_text="Acme Corp announced results today.",
    )
    stub = StubExtractor(
        {variant.contextualized_text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]}
    )

    _, [mention] = _extract_chunk(session, chunk=chunk, extractor=stub)

    assert mention.anchor_kind == "revision"
    assert mention.source_span_start is None
    assert mention.source_span_end is None
    # The near-miss form is present in the raw text, but the exact surface is not —
    # so a fuzzy or positional match would have wrongly source-anchored it here.
    assert near_miss in chunk.text
    assert "Acme Corp" not in chunk.text


# --------------------------------------------------------------------------- #
# EE3 — pair / singleton / cross-chunk co-occurrence partition
# --------------------------------------------------------------------------- #


def test_cooccurrence_pair_mutual(session: Session) -> None:
    """Two mentions in one chunk co-occur mutually, and neither co-occurs with itself."""
    chunk = _seed_chunk(session, chunk_id="c1", text="Acme Corp met Globex Inc today.")
    stub = StubExtractor(
        {
            chunk.text: [
                Detection(surface_form="Acme Corp", span_start=0, span_end=9),
                Detection(surface_form="Globex Inc", span_start=14, span_end=24),
            ]
        }
    )

    run, (mention_a, mention_b) = _extract_chunk(session, chunk=chunk, extractor=stub)

    assert _cooccurring_ids(session, run.id, mention_a.mention_id) == {mention_b.mention_id}
    assert _cooccurring_ids(session, run.id, mention_b.mention_id) == {mention_a.mention_id}


def test_singleton_no_cooccurrence(session: Session) -> None:
    """A chunk from which exactly one mention is emitted has no co-occurrences."""
    chunk = _seed_chunk(session, chunk_id="c1", text="Acme Corp announced results.")
    stub = StubExtractor({chunk.text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]})

    run, [mention] = _extract_chunk(session, chunk=chunk, extractor=stub)

    assert _cooccurring_ids(session, run.id, mention.mention_id) == set()


def test_cross_chunk_no_cooccurrence(session: Session) -> None:
    """Mentions extracted from two different chunks in the same run do not co-occur."""
    chunk1 = _seed_chunk(session, chunk_id="c1", text="Acme Corp announced results.")
    chunk2 = _seed_chunk(session, chunk_id="c2", text="Globex Inc announced results.", ordinal=1)
    stub = StubExtractor(
        {
            chunk1.text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)],
            chunk2.text: [Detection(surface_form="Globex Inc", span_start=0, span_end=10)],
        }
    )

    run1, [mention1] = _extract_chunk(session, chunk=chunk1, extractor=stub)
    run2, [mention2] = _extract_chunk(session, chunk=chunk2, extractor=stub)

    assert run1.id == run2.id, "both chunks share one source, so one extraction run"
    assert _cooccurring_ids(session, run1.id, mention1.mention_id) == set()
    assert _cooccurring_ids(session, run2.id, mention2.mention_id) == set()


# --------------------------------------------------------------------------- #
# EE5 — available / absent / present-empty input-selection partition
# --------------------------------------------------------------------------- #


def test_extracts_from_variant_when_available(session: Session) -> None:
    """A chunk with a variant in its document's active contextualization run is resolved to read it."""
    chunk = _seed_chunk(session, chunk_id="c1", text="The firm announced results.")
    variant_run = _seed_variant_run(session, source_id=chunk.source_id, derivation_key="variant-key-1")
    variant = _seed_contextualized_chunk(
        session,
        chunk_id=chunk.chunk_id,
        run_id=variant_run.id,
        contextualized_text="Acme Corp announced results.",
        context_version=2,
    )

    resolved = select_extraction_input(session, chunk)

    assert resolved == ExtractionInput(
        text=variant.contextualized_text,
        input_kind="contextualized",
        context_version=2,
        contextualization_run_id=variant_run.id,
    )


def test_falls_back_to_raw(session: Session) -> None:
    """A chunk with no active-run variant (none produced, or only a superseded one) resolves to raw text."""
    chunk = _seed_chunk(session, chunk_id="c1", text="The firm announced results.")

    assert select_extraction_input(session, chunk) == ExtractionInput(text=chunk.text, input_kind="raw")

    # A variant exists, but only under a run that is then superseded: still ignored.
    stale_run = _seed_variant_run(session, source_id=chunk.source_id, derivation_key="stale-key")
    _seed_contextualized_chunk(
        session, chunk_id=chunk.chunk_id, run_id=stale_run.id, contextualized_text="Acme Corp announced results."
    )
    _seed_variant_run(session, source_id=chunk.source_id, derivation_key="fresh-key")
    session.refresh(stale_run)
    assert stale_run.status == RunStatus.SUPERSEDED

    assert select_extraction_input(session, chunk) == ExtractionInput(text=chunk.text, input_kind="raw")


def test_present_empty_variant_consumed_as_raw(session: Session) -> None:
    """A present-but-empty active variant is consumed as raw text and persists cleanly end-to-end.

    :func:`~aizk.graph.mention_store.persist_mentions` rejects a contextualized
    draft whose variant is present-empty (the already-self-contained case), so a
    conformant extractor pipeline must never build one — this proves the full
    ``select_extraction_input`` -> ``extract_chunk_mentions`` -> ``persist_mentions``
    path stays raw for such a chunk.
    """
    chunk = _seed_chunk(session, chunk_id="c1", text="Acme Corp announced results today.")
    variant_run = _seed_variant_run(session, source_id=chunk.source_id, derivation_key="variant-key-1")
    _seed_contextualized_chunk(session, chunk_id=chunk.chunk_id, run_id=variant_run.id, contextualized_text="")
    stub = StubExtractor({chunk.text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)]})

    _, [mention] = _extract_chunk(session, chunk=chunk, extractor=stub)

    assert mention.input_kind == "raw"
    assert mention.anchor_kind == "source"
    assert json.loads(mention.input_ref) == {"chunk_id": chunk.chunk_id}


# --------------------------------------------------------------------------- #
# EE7 — substitutable extractor
# --------------------------------------------------------------------------- #


def test_substitute_extractor_drives_run_unchanged(session: Session) -> None:
    """A second, independent EntityExtractor implementation drives identical mention shape/spans/provenance."""

    class _OtherExtractor:
        """A second, independent :class:`EntityExtractor` implementation with the same detections."""

        def __init__(self, detections_by_text: dict[str, list[Detection]], *, extractor_version: str) -> None:
            self._detections_by_text = detections_by_text
            self.extractor_version = extractor_version

        def extract(self, text: str) -> "Sequence[Detection]":
            return list(self._detections_by_text.get(text, []))

    chunk_text = "Acme Corp met Globex Inc today."
    detections = {
        chunk_text: [
            Detection(surface_form="Acme Corp", span_start=0, span_end=9),
            Detection(surface_form="Globex Inc", span_start=14, span_end=24),
        ]
    }
    stub_a = StubExtractor(detections, extractor_version="extractor/v1")
    stub_b = _OtherExtractor(detections, extractor_version="extractor/v1")

    chunk_a = _seed_chunk(session, chunk_id="c1", text=chunk_text, source_id=_SOURCE_A)
    chunk_b = _seed_chunk(session, chunk_id="c2", text=chunk_text, source_id=_SOURCE_B)
    _, mentions_a = _extract_chunk(session, chunk=chunk_a, extractor=stub_a, upstream_derivation_key="key")
    _, mentions_b = _extract_chunk(session, chunk=chunk_b, extractor=stub_b, upstream_derivation_key="key")

    def _shape(mentions: list[Mention]) -> list[tuple[str, str, str, int | None, int | None]]:
        return sorted(
            (m.surface_form, m.anchor_kind, m.input_kind, m.source_span_start, m.source_span_end) for m in mentions
        )

    assert _shape(mentions_a) == _shape(mentions_b)


def test_all_extractor_calls_through_single_access_point(session: Session) -> None:
    """Every extractor invocation the stage makes is observed at the single access point, and none are made outside it."""
    chunk1 = _seed_chunk(session, chunk_id="c1", text="Acme Corp is here.")
    chunk2 = _seed_chunk(session, chunk_id="c2", text="Globex Inc is here.", ordinal=1)
    inner = StubExtractor(
        {
            chunk1.text: [Detection(surface_form="Acme Corp", span_start=0, span_end=9)],
            chunk2.text: [Detection(surface_form="Globex Inc", span_start=0, span_end=10)],
        }
    )
    recorder = RecordingExtractor(inner)

    _extract_chunk(session, chunk=chunk1, extractor=recorder)
    _extract_chunk(session, chunk=chunk2, extractor=recorder)

    assert recorder.calls == [chunk1.text, chunk2.text]
