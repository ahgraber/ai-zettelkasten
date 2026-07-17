"""Behavioral tests for the mention store's write path (``aizk.graph.mention_store``).

Exercises :func:`persist_mentions` and its run helpers against a real (file-based)
SQLite schema carrying the chunk, contextualized-chunk, mention, co-occurrence, and
shared ``pipeline_runs`` tables: append-only immutability, per-source run
supersession and isolation, run-scoped within-run idempotency versus cross-run
``source_occurrence_key`` stability, complete provenance with no embedding or
blocking-key column, source/revision anchor-class span behavior, co-occurrence
link resolution and retry-idempotency, and boundary validation of provenance
(run congruence, chunk resolution, source scoping, contextualized-locator
resolution, and span integrity) before any write.

Uses a lightweight ``create_all``-built schema scoped to exactly the tables this
module's write path touches (mirroring the sibling migration-fidelity suite,
which instead asserts migrated-vs-``create_all`` schema equivalence).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
import pytest
from sqlalchemy import Engine, inspect
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.graph.datamodel import Chunk, ChunkRunManifest, ContextualizedChunk, Mention, MentionCooccurrence
from aizk.graph.extraction import Detection
from aizk.graph.extraction_run import extract_document
from aizk.graph.mention_store import (
    MentionDraft,
    active_extraction_run,
    extraction_derivation_key,
    open_extraction_run,
    persist_mentions,
)
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


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """Return a file-based SQLite URL for a per-test database."""
    return f"sqlite:///{tmp_path / 'mentions.db'}"


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


def _seed_contextualized_chunk(
    session: Session,
    *,
    chunk_id: str,
    contextualized_text: str,
    run_id: int = 999,
    context_version: int = 1,
    summary_run_id: int = 1,
    chunking_run_id: int = 1,
) -> ContextualizedChunk:
    """Insert and commit a :class:`ContextualizedChunk` row, the real locator target for contextualized drafts."""
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


def _open_run(
    session: Session,
    *,
    source_id: str = _SOURCE_A,
    upstream_derivation_key: str = "key-1",
    extractor_version: str = "stub-v1",
) -> PipelineRun:
    """Open an extraction run and commit it (the store functions themselves never commit)."""
    run = open_extraction_run(
        session,
        source_id=source_id,
        extractor_version=extractor_version,
        materializer_version="1",
        input_policy="raw",
        upstream_derivation_key=upstream_derivation_key,
    )
    session.commit()
    session.refresh(run)
    return run


def _source_draft(
    *,
    chunk_id: str,
    surface_form: str,
    span: tuple[int, int],
    input_span: tuple[int, int] | None = None,
) -> MentionDraft:
    """Build a source-anchored, raw-input draft for ``surface_form`` at ``span`` in ``chunk_id``."""
    input_span = input_span or span
    return MentionDraft(
        chunk_id=chunk_id,
        anchor_kind="source",
        surface_form=surface_form,
        input_kind="raw",
        input_span_start=input_span[0],
        input_span_end=input_span[1],
        source_span_start=span[0],
        source_span_end=span[1],
        source_anchor_text=surface_form,
    )


def _revision_draft(
    *,
    chunk_id: str,
    surface_form: str,
    context_version: int = 1,
    contextualization_run_id: int = 999,
    input_span: tuple[int, int] = (0, 5),
) -> MentionDraft:
    """Build a revision-anchored, contextualized-input draft for ``surface_form`` in ``chunk_id``."""
    return MentionDraft(
        chunk_id=chunk_id,
        anchor_kind="revision",
        surface_form=surface_form,
        input_kind="contextualized",
        context_version=context_version,
        contextualization_run_id=contextualization_run_id,
        input_span_start=input_span[0],
        input_span_end=input_span[1],
    )


def _persist(session: Session, *, run: PipelineRun, drafts: "Sequence[MentionDraft]") -> list[Mention]:
    """Call :func:`persist_mentions` and commit, mirroring how a caller owns the transaction."""
    persisted = persist_mentions(session, run=run, mentions=drafts)
    session.commit()
    return persisted


# --------------------------------------------------------------------------- #
# open_extraction_run: key + stamps derived together
# --------------------------------------------------------------------------- #


def test_open_extraction_run_records_key_and_stamps_from_same_inputs(session: Session) -> None:
    """The opened run's derivation_key and version_stamps are both derived from the semantic inputs supplied."""
    run = open_extraction_run(
        session,
        source_id=_SOURCE_A,
        extractor_version="spacy-en_core_web_sm-3.7-v1",
        materializer_version="2",
        input_policy="contextualized",
        upstream_derivation_key="upstream-key",
    )
    session.commit()
    session.refresh(run)

    assert run.derivation_key == extraction_derivation_key(
        extractor_version="spacy-en_core_web_sm-3.7-v1",
        materializer_version="2",
        input_policy="contextualized",
        upstream_derivation_key="upstream-key",
    )
    assert json.loads(run.version_stamps_json) == {
        "extractor_version": "spacy-en_core_web_sm-3.7-v1",
        "materializer_version": "2",
        "input_policy": "contextualized",
    }


# --------------------------------------------------------------------------- #
# Append-only / run supersession / per-source isolation
# --------------------------------------------------------------------------- #


def test_mentions_append_only(session: Session) -> None:
    """A persisted mention is never mutated when its source is re-extracted."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    run1 = _open_run(session, upstream_derivation_key="key-1")
    draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))
    [original] = _persist(session, run=run1, drafts=[draft])
    session.refresh(original)
    original_state = original.model_dump()

    # Re-extract the source with a changed derivation key: opens a superseding run.
    run2 = _open_run(session, upstream_derivation_key="key-2")
    assert run2.id != run1.id
    _persist(session, run=run2, drafts=[draft])

    reloaded = session.get(Mention, original.mention_id)
    assert reloaded is not None
    assert reloaded.model_dump() == original_state, "the original mention record must remain unchanged"


def test_re_extraction_supersedes_and_retains(session: Session) -> None:
    """A new run for a source supersedes its prior run; another source is untouched."""
    _seed_chunk(session, chunk_id="chunk-a1", text="Acme Corp is a company.", source_id=_SOURCE_A)
    _seed_chunk(session, chunk_id="chunk-b1", text="Globex Inc is a company.", source_id=_SOURCE_B)

    run_a1 = _open_run(session, source_id=_SOURCE_A, upstream_derivation_key="a-key-1")
    draft_a = _source_draft(chunk_id="chunk-a1", surface_form="Acme Corp", span=(0, 9))
    _persist(session, run=run_a1, drafts=[draft_a])

    run_b1 = _open_run(session, source_id=_SOURCE_B, upstream_derivation_key="b-key-1")
    draft_b = _source_draft(chunk_id="chunk-b1", surface_form="Globex Inc", span=(0, 10))
    _persist(session, run=run_b1, drafts=[draft_b])

    # Re-extract source A only.
    run_a2 = _open_run(session, source_id=_SOURCE_A, upstream_derivation_key="a-key-2")
    _persist(session, run=run_a2, drafts=[draft_a])

    session.refresh(run_a1)
    session.refresh(run_a2)
    session.refresh(run_b1)
    assert run_a1.status == RunStatus.SUPERSEDED
    assert run_a2.status == RunStatus.ACTIVE
    assert active_extraction_run(session, _SOURCE_A).id == run_a2.id

    # Source A's prior run's mentions remain present.
    prior_mentions = session.exec(select(Mention).where(Mention.run_id == run_a1.id)).all()
    assert len(prior_mentions) == 1

    # Source B's run and mentions are untouched.
    assert run_b1.status == RunStatus.ACTIVE
    assert active_extraction_run(session, _SOURCE_B).id == run_b1.id
    b_mentions = session.exec(select(Mention).where(Mention.run_id == run_b1.id)).all()
    assert len(b_mentions) == 1


# --------------------------------------------------------------------------- #
# Run-scoped identity / cross-run occurrence key stability
# --------------------------------------------------------------------------- #


def test_run_scoped_rows_distinct_occurrence_key_stable(session: Session) -> None:
    """Same source occurrence under two runs -> two rows, equal occurrence keys; no in-run duplicate."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))

    run1 = _open_run(session, upstream_derivation_key="key-1")
    [mention1] = _persist(session, run=run1, drafts=[draft])

    run2 = _open_run(session, upstream_derivation_key="key-2")
    [mention2] = _persist(session, run=run2, drafts=[draft])

    assert mention1.mention_id != mention2.mention_id
    assert mention1.run_id != mention2.run_id
    assert mention1.source_occurrence_key == mention2.source_occurrence_key

    # Re-persisting within run2 does not duplicate.
    [mention2_again] = _persist(session, run=run2, drafts=[draft])
    assert mention2_again.mention_id == mention2.mention_id
    rows_in_run2 = session.exec(select(Mention).where(Mention.run_id == run2.id)).all()
    assert len(rows_in_run2) == 1


# --------------------------------------------------------------------------- #
# Provenance completeness / no embedding
# --------------------------------------------------------------------------- #


def test_provenance_and_spans_present_no_embedding(session: Session) -> None:
    """Every mention has full provenance; source_chunk_span iff source-anchored; no embedding or blocking-key column."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp met Globex Inc today.")
    _seed_contextualized_chunk(session, chunk_id="chunk-1", contextualized_text="Globex Inc appears here.")
    run = _open_run(session)
    source_draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))
    revision_draft = _revision_draft(chunk_id="chunk-1", surface_form="Globex Inc", input_span=(0, 10))

    persisted = _persist(session, run=run, drafts=[source_draft, revision_draft])

    for mention in persisted:
        assert mention.surface_form
        assert mention.chunk_id
        assert mention.anchor_kind in ("source", "revision")
        assert mention.input_kind in ("raw", "contextualized")
        assert mention.input_ref
        assert mention.input_span_start is not None
        assert mention.input_span_end is not None

    source_mention, revision_mention = persisted
    assert source_mention.anchor_kind == "source"
    assert source_mention.source_span_start is not None
    assert source_mention.source_span_end is not None
    assert source_mention.source_occurrence_key is not None

    assert revision_mention.anchor_kind == "revision"
    assert revision_mention.source_span_start is None
    assert revision_mention.source_span_end is None
    assert revision_mention.source_occurrence_key is None

    column_names = {c.name for c in inspect(Mention).columns}
    assert not any("embedding" in name.lower() for name in column_names), (
        f"Mention table must not carry an embedding-like column, found: {column_names}"
    )
    assert not any("blocking" in name.lower() for name in column_names), (
        f"Mention table must not carry a blocking-key column (candidate-generation keys are "
        f"derived downstream from surface_form), found: {column_names}"
    )


# --------------------------------------------------------------------------- #
# Source / revision / expansion partition
# --------------------------------------------------------------------------- #


def test_source_anchor_resolves(session: Session) -> None:
    """A source-anchored mention's span, sliced against the raw chunk text, equals its surface_form."""
    chunk_text = "Acme Corp announced quarterly results today."
    _seed_chunk(session, chunk_id="chunk-1", text=chunk_text)
    run = _open_run(session)
    draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))

    [mention] = _persist(session, run=run, drafts=[draft])

    chunk_row = session.get(Chunk, mention.chunk_id)
    assert chunk_row is not None
    sliced = chunk_row.text[mention.source_span_start : mention.source_span_end]
    assert sliced == mention.surface_form == "Acme Corp"


def test_revision_only_name_persisted_without_span(session: Session) -> None:
    """A revision-anchored draft (name absent from raw text) persists with no span/occurrence key."""
    _seed_chunk(session, chunk_id="chunk-1", text="The company announced results today.")
    _seed_contextualized_chunk(
        session,
        chunk_id="chunk-1",
        contextualized_text="Acme Corp announced results today.",
        run_id=42,
        context_version=2,
    )
    run = _open_run(session)
    draft = _revision_draft(
        chunk_id="chunk-1",
        surface_form="Acme Corp",
        context_version=2,
        contextualization_run_id=42,
        input_span=(0, 9),
    )

    [mention] = _persist(session, run=run, drafts=[draft])

    assert mention.anchor_kind == "revision"
    assert mention.source_span_start is None
    assert mention.source_span_end is None
    assert mention.source_occurrence_key is None
    assert mention.input_kind == "contextualized"
    assert mention.input_ref
    assert '"context_version":2' in mention.input_ref
    assert '"run_id":42' in mention.input_ref


def test_repeated_surface_one_mention_per_occurrence(session: Session) -> None:
    """A surface form occurring three times yields three source-anchored rows, each with its own span."""
    chunk_text = "Acme Corp met Acme Corp's rival, who then called Acme Corp again."
    _seed_chunk(session, chunk_id="chunk-1", text=chunk_text)
    run = _open_run(session)

    surface = "Acme Corp"
    spans: list[tuple[int, int]] = []
    cursor = 0
    for _ in range(3):
        start = chunk_text.index(surface, cursor)
        spans.append((start, start + len(surface)))
        cursor = start + 1
    assert len(spans) == 3
    for start, end in spans:
        assert chunk_text[start:end] == surface
    drafts = [_source_draft(chunk_id="chunk-1", surface_form=surface, span=span) for span in spans]

    persisted = _persist(session, run=run, drafts=drafts)

    assert len(persisted) == 3
    assert len({m.mention_id for m in persisted}) == 3
    assert {(m.source_span_start, m.source_span_end) for m in persisted} == set(spans)
    assert all(m.anchor_kind == "source" for m in persisted)


def test_source_and_revision_share_surface_coexist(session: Session) -> None:
    """A source-anchored and a revision-anchored mention with one (chunk, surface_form) are distinct rows.

    The two anchor classes have separate within-run identities, so a
    source-anchored row must never shadow a revision draft sharing its
    ``(run_id, chunk_id, surface_form)`` tuple (or vice versa): both persist,
    one per class, and the returned list maps each draft to its own row.
    """
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    _seed_contextualized_chunk(session, chunk_id="chunk-1", contextualized_text="Acme Corp is mentioned again.")
    run = _open_run(session)
    source_draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))
    revision_draft = _revision_draft(chunk_id="chunk-1", surface_form="Acme Corp", input_span=(0, 9))

    source_mention, revision_mention = _persist(session, run=run, drafts=[source_draft, revision_draft])

    assert source_mention.mention_id != revision_mention.mention_id
    assert source_mention.anchor_kind == "source"
    assert revision_mention.anchor_kind == "revision"
    rows = session.exec(select(Mention).where(Mention.run_id == run.id)).all()
    assert len(rows) == 2
    assert {m.anchor_kind for m in rows} == {"source", "revision"}


# --------------------------------------------------------------------------- #
# Co-occurrence
# --------------------------------------------------------------------------- #


def test_cooccurrence_resolvable_off_row(session: Session) -> None:
    """Two mentions in one chunk: querying the link table from either endpoint returns the other."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp met Globex Inc today.")
    run = _open_run(session)
    draft_a = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))
    draft_b = _source_draft(chunk_id="chunk-1", surface_form="Globex Inc", span=(14, 24))

    mention_a, mention_b = _persist(session, run=run, drafts=[draft_a, draft_b])

    links = session.exec(select(MentionCooccurrence).where(MentionCooccurrence.run_id == run.id)).all()
    assert len(links) == 1
    link = links[0]
    endpoints = {link.mention_id_lo, link.mention_id_hi}
    assert endpoints == {mention_a.mention_id, mention_b.mention_id}
    assert link.chunk_id == "chunk-1"

    # Resolvable from either endpoint's column, without a co-occurrence field on the mention row.
    from_lo = session.exec(
        select(MentionCooccurrence).where(MentionCooccurrence.mention_id_lo == link.mention_id_lo)
    ).all()
    from_hi = session.exec(
        select(MentionCooccurrence).where(MentionCooccurrence.mention_id_hi == link.mention_id_hi)
    ).all()
    assert from_lo and from_hi

    mention_columns = {c.name for c in inspect(Mention).columns}
    assert not any("cooccur" in name.lower() for name in mention_columns)


def test_cooccurrence_retry_does_not_duplicate(session: Session) -> None:
    """Persisting a chunk's mentions twice within one run leaves each pair recorded exactly once, lo < hi."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp met Globex Inc and Initech today.")
    run = _open_run(session)
    drafts = [
        _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9)),
        _source_draft(chunk_id="chunk-1", surface_form="Globex Inc", span=(14, 24)),
        _source_draft(chunk_id="chunk-1", surface_form="Initech", span=(29, 36)),
    ]

    _persist(session, run=run, drafts=drafts)
    # Retry: persist the same chunk's mentions and links again within the same run.
    _persist(session, run=run, drafts=drafts)

    links = session.exec(select(MentionCooccurrence).where(MentionCooccurrence.run_id == run.id)).all()
    # C(3, 2) = 3 unordered pairs.
    assert len(links) == 3
    pairs = {(link.mention_id_lo, link.mention_id_hi) for link in links}
    assert len(pairs) == 3
    assert all(lo < hi for lo, hi in pairs), "every stored pair must be in canonical lo < hi order"


# --------------------------------------------------------------------------- #
# Chunk resolvability
# --------------------------------------------------------------------------- #


def test_chunk_id_resolves(session: Session) -> None:
    """A persisted mention's chunk_id looks up an existing Chunk row."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    run = _open_run(session)
    draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))

    [mention] = _persist(session, run=run, drafts=[draft])

    chunk_row = session.get(Chunk, mention.chunk_id)
    assert chunk_row is not None
    assert chunk_row.chunk_id == "chunk-1"


# --------------------------------------------------------------------------- #
# Boundary validation: rejected before any write
# --------------------------------------------------------------------------- #


def test_persist_mentions_rejects_wrong_stage_run(session: Session) -> None:
    """A run belonging to another stage is refused; no mention row is written."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    other_stage_run = record_run(session, stage="chunking", scope_id=_SOURCE_A, derivation_key="k")
    session.commit()
    session.refresh(other_stage_run)
    draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))

    with pytest.raises(ValueError, match="not an active"):
        persist_mentions(session, run=other_stage_run, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


def test_persist_mentions_rejects_superseded_run(session: Session) -> None:
    """A superseded extraction run is refused; the prior run's mentions are unaffected."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    run1 = _open_run(session, upstream_derivation_key="key-1")
    draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9))
    _persist(session, run=run1, drafts=[draft])

    run2 = _open_run(session, upstream_derivation_key="key-2")
    session.refresh(run1)
    assert run1.status == RunStatus.SUPERSEDED

    with pytest.raises(ValueError, match="not an active"):
        persist_mentions(session, run=run1, mentions=[draft])

    rows_in_run1 = session.exec(select(Mention).where(Mention.run_id == run1.id)).all()
    assert len(rows_in_run1) == 1
    rows_in_run2 = session.exec(select(Mention).where(Mention.run_id == run2.id)).all()
    assert rows_in_run2 == []


def test_persist_mentions_rejects_unknown_chunk_id(session: Session) -> None:
    """A draft whose chunk_id has no persisted Chunk row is refused; no mention row is written."""
    run = _open_run(session)
    draft = _source_draft(chunk_id="does-not-exist", surface_form="Acme Corp", span=(0, 9))

    with pytest.raises(ValueError, match="does-not-exist"):
        persist_mentions(session, run=run, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


def test_persist_mentions_rejects_cross_source_chunk(session: Session) -> None:
    """A draft whose chunk belongs to a different source than the run's scope is refused."""
    _seed_chunk(session, chunk_id="chunk-b1", text="Globex Inc is a company.", source_id=_SOURCE_B)
    run_a = _open_run(session, source_id=_SOURCE_A)
    draft = _source_draft(chunk_id="chunk-b1", surface_form="Globex Inc", span=(0, 10))

    with pytest.raises(ValueError, match="source other than"):
        persist_mentions(session, run=run_a, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


def test_persist_mentions_rejects_source_span_not_matching_surface_and_anchor(session: Session) -> None:
    """A source span that slices to text other than surface_form/source_anchor_text is refused."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    run = _open_run(session)
    # Span (0, 9) covers "Acme Corp" in the chunk text, but the draft claims a
    # different surface form for that span.
    draft = MentionDraft(
        chunk_id="chunk-1",
        anchor_kind="source",
        surface_form="Globex Inc",
        input_kind="raw",
        input_span_start=0,
        input_span_end=9,
        source_span_start=0,
        source_span_end=9,
        source_anchor_text="Globex Inc",
    )

    with pytest.raises(ValueError, match="source-anchored"):
        persist_mentions(session, run=run, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


def test_persist_mentions_rejects_inverted_source_span(session: Session) -> None:
    """A source span whose start is not strictly before its end is refused."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    run = _open_run(session)
    draft = MentionDraft(
        chunk_id="chunk-1",
        anchor_kind="source",
        surface_form="Acme Corp",
        input_kind="raw",
        input_span_start=9,
        input_span_end=0,
        source_span_start=9,
        source_span_end=0,
        source_anchor_text="Acme Corp",
    )

    with pytest.raises(ValueError, match="source-anchored"):
        persist_mentions(session, run=run, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


def test_persist_mentions_rejects_out_of_bounds_source_span(session: Session) -> None:
    """A source span extending past the raw chunk text's length is refused."""
    chunk_text = "Acme Corp announced results."
    _seed_chunk(session, chunk_id="chunk-1", text=chunk_text)
    run = _open_run(session)
    draft = MentionDraft(
        chunk_id="chunk-1",
        anchor_kind="source",
        surface_form="Acme Corp",
        input_kind="raw",
        input_span_start=0,
        input_span_end=len(chunk_text) + 10,
        source_span_start=0,
        source_span_end=len(chunk_text) + 10,
        source_anchor_text="Acme Corp",
    )

    with pytest.raises(ValueError, match="source-anchored"):
        persist_mentions(session, run=run, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


def test_persist_mentions_rejects_input_span_out_of_bounds(session: Session) -> None:
    """An input span extending past the consumed input text's length is refused."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    run = _open_run(session)
    draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9), input_span=(0, 999))

    with pytest.raises(ValueError, match="input_span"):
        persist_mentions(session, run=run, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


def test_persist_mentions_rejects_input_span_not_matching_surface(session: Session) -> None:
    """An input span that slices to text other than surface_form is refused."""
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results.")
    run = _open_run(session)
    # input_span (10, 18) covers "announced", not the declared surface_form.
    draft = _source_draft(chunk_id="chunk-1", surface_form="Acme Corp", span=(0, 9), input_span=(10, 18))

    with pytest.raises(ValueError, match="input_span"):
        persist_mentions(session, run=run, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


def test_persist_mentions_rejects_dangling_contextualized_locator(session: Session) -> None:
    """A contextualized draft whose locator has no matching ContextualizedChunk row is refused."""
    _seed_chunk(session, chunk_id="chunk-1", text="The company announced results today.")
    run = _open_run(session)
    # No ContextualizedChunk row seeded for this (chunk_id, run_id, context_version).
    draft = _revision_draft(
        chunk_id="chunk-1", surface_form="Acme Corp", context_version=1, contextualization_run_id=999
    )

    with pytest.raises(ValueError, match="ContextualizedChunk"):
        persist_mentions(session, run=run, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


def test_mention_draft_rejects_empty_surface_form() -> None:
    """An empty surface_form is refused at the draft boundary, before persistence is ever reached."""
    with pytest.raises(ValidationError):
        MentionDraft(
            chunk_id="chunk-1",
            anchor_kind="source",
            surface_form="",
            input_kind="raw",
            input_span_start=0,
            input_span_end=0,
            source_span_start=0,
            source_span_end=0,
            source_anchor_text="",
        )


def test_persist_mentions_rejects_contextualized_draft_for_present_empty_variant(session: Session) -> None:
    """A contextualized draft whose variant is present-empty is refused: extraction reads raw text in that case.

    A present-empty variant (``contextualized_text = ''``) means the chunk was
    already self-contained and extraction consumed the raw chunk text, so a
    conformant extractor records the mention with raw ``input_kind`` and the chunk
    as its ``input_ref`` — a contextualized ``input_ref`` here would dereference to
    empty text.
    """
    _seed_chunk(session, chunk_id="chunk-1", text="Acme Corp announced results today.")
    _seed_contextualized_chunk(
        session,
        chunk_id="chunk-1",
        contextualized_text="",
        run_id=7,
        context_version=1,
    )
    run = _open_run(session)
    draft = _revision_draft(
        chunk_id="chunk-1",
        surface_form="Acme Corp",
        context_version=1,
        contextualization_run_id=7,
        input_span=(0, 9),
    )

    with pytest.raises(ValueError, match="present-empty"):
        persist_mentions(session, run=run, mentions=[draft])

    assert session.exec(select(Mention)).all() == []


# --------------------------------------------------------------------------- #
# Boundary validation: accepted positive cases
# --------------------------------------------------------------------------- #


def test_persist_mentions_accepts_contextualized_draft_against_real_variant(session: Session) -> None:
    """A contextualized draft validates against a real persisted ContextualizedChunk row."""
    _seed_chunk(session, chunk_id="chunk-1", text="The company announced results today.")
    _seed_contextualized_chunk(
        session,
        chunk_id="chunk-1",
        contextualized_text="Acme Corp announced results today.",
        run_id=42,
        context_version=3,
    )
    run = _open_run(session)
    draft = _revision_draft(
        chunk_id="chunk-1",
        surface_form="Acme Corp",
        context_version=3,
        contextualization_run_id=42,
        input_span=(0, 9),
    )

    [mention] = _persist(session, run=run, drafts=[draft])

    assert mention.anchor_kind == "revision"
    assert mention.input_kind == "contextualized"


# --------------------------------------------------------------------------- #
# Partial-failure atomicity, driven through the real extract_document unit
# --------------------------------------------------------------------------- #


class _StubExtractor:
    """Deterministic EntityExtractor test double returning configured detections per input text.

    Local to this test, mirroring ``tests/graph/test_extraction.py``'s StubExtractor.
    """

    def __init__(self, detections_by_text: dict[str, list[Detection]], *, extractor_version: str = "stub/v1") -> None:
        """Store the fixed ``text -> detections`` mapping and ``extractor_version``."""
        self._detections_by_text = detections_by_text
        self.extractor_version = extractor_version

    def extract(self, text: str) -> "Sequence[Detection]":
        """Return the configured detections for ``text`` (empty if unconfigured)."""
        return list(self._detections_by_text.get(text, []))


def _seed_chunking_run(
    session: Session,
    *,
    source_id: str,
    chunk_texts: "Sequence[tuple[str, str]]",
    derivation_key: str = "chunking-key-1",
) -> PipelineRun:
    """Seed Chunk rows plus a chunking run and manifest, the shape ``extract_document`` reads.

    Bypasses ``persist_chunks`` (which also records ``ChunkRunInput`` and indexes
    raw content, neither of which ``extract_document`` reads) so the test only
    builds what :func:`~aizk.graph.persistence.document_order_chunks` needs: Chunk
    rows and manifest entries carrying document order via ``span_start``.
    """
    run = record_run(session, stage=CHUNKING_STAGE, scope_id=source_id, derivation_key=derivation_key)
    cursor = 0
    for ordinal, (chunk_id, text) in enumerate(chunk_texts):
        session.add(
            Chunk(
                chunk_id=chunk_id,
                content_hash=xxhash.xxh64(text.encode("utf-8")).hexdigest(),
                source_id=source_id,
                heading_path_json="[]",
                ordinal=ordinal,
                text=text,
                char_count=len(text),
            )
        )
        session.add(ChunkRunManifest(run_id=run.id, chunk_id=chunk_id, span_start=cursor, span_end=cursor + len(text)))
        cursor += len(text) + 1
    session.commit()
    session.refresh(run)
    return run


def test_partial_failure_exposes_no_active_run(
    session: Session, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document extraction forced to fail mid-persist leaves no newly-active run and no readable mentions.

    Driven through the real :func:`~aizk.graph.extraction_run.extract_document`
    unit (not a hand-rolled transaction). A first, successful extraction seeds an
    active run with mentions; a second attempt under a differing
    ``extractor_version`` (so :func:`open_extraction_run` would otherwise open a
    superseding run) is forced to fail inside ``persist_mentions``'s
    co-occurrence linking, after its mention rows are already staged in-session.
    The whole write-phase transaction rolls back: the prior run remains active
    and none of the second attempt's rows become readable.
    """
    source_id = _SOURCE_A
    chunk_text = "Acme Corp met Globex Inc today."
    _seed_chunking_run(session, source_id=source_id, chunk_texts=[("chunk-1", chunk_text)])
    detections = {
        chunk_text: [
            Detection(surface_form="Acme Corp", span_start=0, span_end=9),
            Detection(surface_form="Globex Inc", span_start=14, span_end=24),
        ]
    }

    first = extract_document(
        engine,
        source_id=source_id,
        extractor=_StubExtractor(detections, extractor_version="stub/v1"),
        input_policy="raw",
    )
    assert first.mention_count == 2
    prior_run = active_extraction_run(session, source_id)
    assert prior_run is not None
    prior_run_id = prior_run.id
    prior_mention_ids = {
        m.mention_id for m in session.exec(select(Mention).where(Mention.run_id == prior_run_id)).all()
    }
    assert len(prior_mention_ids) == 2

    def _always_fail(session: Session, *, run_id: int, chunk_id: str) -> None:
        raise RuntimeError("forced failure mid-persist")

    monkeypatch.setattr("aizk.graph.mention_store._link_chunk_cooccurrences", _always_fail)

    with pytest.raises(RuntimeError, match="forced failure mid-persist"):
        extract_document(
            engine,
            source_id=source_id,
            extractor=_StubExtractor(detections, extractor_version="stub/v2"),
            input_policy="raw",
        )

    current_run = active_extraction_run(session, source_id)
    assert current_run is not None
    assert current_run.id == prior_run_id, "no newly-active run for the source"
    session.refresh(current_run)
    assert current_run.status == RunStatus.ACTIVE, "the prior active run remains active"

    all_mentions = session.exec(select(Mention)).all()
    assert {m.mention_id for m in all_mentions} == prior_mention_ids, (
        "no mentions from the failed attempt are readable"
    )
