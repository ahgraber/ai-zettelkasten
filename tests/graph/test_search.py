"""Behavioral tests for the ranked, type-filtered content search provider.

Exercises :class:`aizk.graph.search.Fts5SearchProvider` end-to-end over the real
persist path (so the index contents are the production ones): a search returns
only active-generation content (superseded chunks dropped, the active chunk kept),
honors the raw/contextualized/either type filter (including a self-contained chunk
matching on the contextualized side by its raw text), aggregates a both-sides match
into one result carrying both per-side flags, orders documents by relevance then
chunks by ``span_start``, treats operator input as literal terms (empty input
yields nothing, query-syntax characters never error), and never surfaces content
for a source with no active variant run (memo exclusion at the search surface).

Model output text is non-deterministic in production, so these drive a
deterministic stub and assert structure, membership, flags, and ordering — never
exact model output.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlmodel import Session
import xxhash

from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk
from aizk.graph.contextualization import contextualize_chunks, summarize_document
from aizk.graph.llm import StubLLMClient
from aizk.graph.persistence import persist_chunks
from aizk.graph.search import (
    MAX_QUERY_LENGTH,
    Fts5SearchProvider,
    SearchKind,
    SearchProvider,
    escape_fts_query,
)


@pytest.fixture
def provider(db_url: str) -> Iterator[Fts5SearchProvider]:
    """An FTS5 provider over the test database via a plain read engine.

    The provider is read-only, so it is given a plain engine on the same database
    file — without the test write fixture's ``BEGIN IMMEDIATE`` listener — mirroring
    production, where the read path is a distinct connection that never takes the
    single writer's write lock. Sharing the write fixture's engine would force the
    provider's read onto a ``BEGIN IMMEDIATE`` write lock and deadlock against the
    open writer.
    """
    eng: Engine = create_engine(db_url, connect_args={"check_same_thread": False, "timeout": 30})
    yield Fts5SearchProvider(eng)
    eng.dispose()


_AIZK_UUID = "11111111-1111-1111-1111-111111111111"
_AIZK_UUID_B = "22222222-2222-2222-2222-222222222222"
_OUTPUT = "output-1"
_HASH_A = "0011223344556677"
_HASH_B = "aabbccddeeff0011"
_DOC_TEXT = "# Title\n\nThe document body the summary pass reads."


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_chunk(
    text_: str,
    *,
    ordinal: int,
    span_start: int = 0,
    source_id: str = _AIZK_UUID,
    markdown_hash: str = _HASH_A,
) -> SplitterChunk:
    """Build a splitter chunk with an explicit ``span_start`` (identity is assigned at persistence)."""
    content_hash = xxhash.xxh64(text_.encode("utf-8")).hexdigest()
    return SplitterChunk(
        content_hash=content_hash,
        source_id=source_id,
        heading_path=(),
        ordinal=ordinal,
        text=text_,
        char_count=len(text_),
        converted_artifact_id=_OUTPUT,
        markdown_hash_xx64=markdown_hash,
        span=(span_start, span_start + len(text_)),
        splitter_version=SPLITTER_VERSION,
    )


def _persist_chunks(
    session: Session,
    chunks: list[SplitterChunk],
    *,
    source_id: str = _AIZK_UUID,
    markdown_hash: str = _HASH_A,
) -> tuple[int, list[SplitterChunk]]:
    """Persist a chunk set; return its chunking run id and the surrogate-bearing chunks."""
    run, persisted = persist_chunks(
        session,
        source_id=source_id,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=markdown_hash,
        splitter_version=SPLITTER_VERSION,
        chunks=chunks,
    )
    session.commit()
    assert run.id is not None
    return run.id, persisted


def _contextualize(
    session: Session,
    chunks: list[SplitterChunk],
    revisions: list[str],
    *,
    chunking_run_id: int,
    source_id: str = _AIZK_UUID,
    markdown_hash: str = _HASH_A,
) -> None:
    """Summarize and contextualize ``chunks`` with the given canned ``revisions``; commit."""
    summary = summarize_document(
        session,
        StubLLMClient(),
        source_id=source_id,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=markdown_hash,
        document_text=_DOC_TEXT,
    )
    contextualize_chunks(
        session,
        StubLLMClient(),
        source_id=source_id,
        summary=summary,
        chunks=chunks,
        chunking_run_id=chunking_run_id,
        splitter_version=SPLITTER_VERSION,
        precomputed_revisions=revisions,
    )
    session.commit()


# --------------------------------------------------------------------------- #
# Protocol / escaping
# --------------------------------------------------------------------------- #


def test_provider_satisfies_protocol(engine) -> None:  # noqa: ANN001
    """The FTS implementation is a structural :class:`SearchProvider`."""
    assert isinstance(Fts5SearchProvider(engine), SearchProvider)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("   ", None),
        ("\t\n  ", None),
        ("attention", '"attention"'),
        ("scaled dot-product", '"scaled" "dot-product"'),
        ('say "hi"', '"say" """hi"""'),
        ("a* OR b", '"a*" "OR" "b"'),
    ],
)
def test_escape_fts_query_produces_literal_terms(raw: str, expected: str | None) -> None:
    """Each whitespace token is double-quoted (quotes doubled); empty input is ``None``."""
    assert escape_fts_query(raw) == expected


def test_escape_fts_query_truncates_overlong_input() -> None:
    """Input beyond the length bound is truncated, never rejected, so a long term still searches."""
    long_term = "x" * (MAX_QUERY_LENGTH + 50)
    escaped = escape_fts_query(long_term)
    assert escaped == f'"{"x" * MAX_QUERY_LENGTH}"'


# --------------------------------------------------------------------------- #
# Active-generation only
# --------------------------------------------------------------------------- #


def test_search_returns_active_chunk_not_superseded(session: Session, provider: Fts5SearchProvider) -> None:
    """A term in a superseded-only chunk is not returned; the active chunk is.

    Generation 1 holds a chunk with a distinctive term; generation 2 (different
    markdown, different manifest set) supersedes run 1 and does not re-emit that
    chunk, but re-emits a chunk carrying a different distinctive term. Searching the
    superseded term returns nothing; searching the active term returns the active
    chunk.
    """
    superseded = _make_chunk("antidisestablishment doctrine", ordinal=0, markdown_hash=_HASH_A)
    _persist_chunks(session, [superseded], markdown_hash=_HASH_A)
    active = _make_chunk("floccinaucinihilipilification practice", ordinal=0, markdown_hash=_HASH_B)
    extra = _make_chunk("filler body text", ordinal=1, markdown_hash=_HASH_B)
    _run_id, persisted = _persist_chunks(session, [active, extra], markdown_hash=_HASH_B)
    active_p = persisted[0]

    assert provider.search("antidisestablishment", SearchKind.CHUNK) == []
    active_hits = provider.search("floccinaucinihilipilification", SearchKind.CHUNK)
    assert [r.chunk_id for r in active_hits] == [active_p.chunk_id]


def test_search_drops_contextualized_hit_when_chunk_left_active_manifest(
    session: Session,
    provider: Fts5SearchProvider,
) -> None:
    """A contextualized hit is dropped once its chunk leaves the active chunking manifest.

    Contextualization (a variant run) supersedes independently of chunking, so a
    re-chunk can supersede the chunking run while the prior variant run stays active
    — leaving its contextualized chunks pointing at a now-superseded chunking
    manifest. Search anchors every hit on the *active* chunking manifest, so such an
    orphaned contextualized hit is dropped (its chunk_id no longer exists in the
    active generation), while the source's active chunks remain searchable.
    """
    # C1 active: a chunk whose contextualization introduces a distinctive term.
    chunk_c1 = _make_chunk("it improves throughput", ordinal=0, markdown_hash=_HASH_A)
    run_c1, persisted_c1 = _persist_chunks(session, [chunk_c1], markdown_hash=_HASH_A)
    chunk_c1_p = persisted_c1[0]
    _contextualize(
        session,
        [chunk_c1_p],
        ["floccinaucinihilipilification improves throughput"],
        chunking_run_id=run_c1,
        markdown_hash=_HASH_A,
    )

    # Sanity: while C1 is active, the contextualized-only term is searchable.
    assert [r.chunk_id for r in provider.search("floccinaucinihilipilification", SearchKind.CONTEXTUALIZED)] == [
        chunk_c1_p.chunk_id
    ]
    assert [r.chunk_id for r in provider.search("floccinaucinihilipilification", SearchKind.EITHER)] == [
        chunk_c1_p.chunk_id
    ]

    # Re-chunk (C2 active, C1 superseded) with a disjoint chunk set that no longer
    # contains chunk_c1, and do NOT re-contextualize — so V1 stays the active variant
    # run while its chunk_id is gone from the active chunking manifest.
    chunk_c2 = _make_chunk("antidisestablishment doctrine", ordinal=0, markdown_hash=_HASH_B)
    _run_c2, persisted_c2 = _persist_chunks(session, [chunk_c2], markdown_hash=_HASH_B)
    chunk_c2_p = persisted_c2[0]

    # The orphaned contextualized hit is dropped: its chunk_id is absent from the
    # active chunking manifest, so no result references a span absent from the doc.
    assert provider.search("floccinaucinihilipilification", SearchKind.CONTEXTUALIZED) == []
    assert provider.search("floccinaucinihilipilification", SearchKind.EITHER) == []

    # The source is not globally broken — C2's active chunk is still searchable.
    assert [r.chunk_id for r in provider.search("antidisestablishment", SearchKind.CHUNK)] == [chunk_c2_p.chunk_id]


# --------------------------------------------------------------------------- #
# Type filter — three partitions
# --------------------------------------------------------------------------- #


def test_search_type_filter_contextualized_only_term(session: Session, provider: Fts5SearchProvider) -> None:
    """A term introduced by contextualization matches under contextualized/either, not chunk."""
    chunk = _make_chunk("it improves throughput", ordinal=0)
    run_id, persisted = _persist_chunks(session, [chunk])
    chunk_p = persisted[0]
    _contextualize(
        session,
        [chunk_p],
        ["scaled dot-product attention improves throughput"],
        chunking_run_id=run_id,
    )

    under_ctx = provider.search("scaled", SearchKind.CONTEXTUALIZED)
    under_either = provider.search("scaled", SearchKind.EITHER)
    under_chunk = provider.search("scaled", SearchKind.CHUNK)

    assert [r.chunk_id for r in under_ctx] == [chunk_p.chunk_id]
    assert under_ctx[0].matched_in_contextualized is True
    assert under_ctx[0].matched_in_chunk is False
    assert [r.chunk_id for r in under_either] == [chunk_p.chunk_id]
    assert under_chunk == []


def test_search_type_filter_raw_only_term(session: Session, provider: Fts5SearchProvider) -> None:
    """A term the revision rephrased away matches under chunk/either, not contextualized."""
    chunk = _make_chunk("the antiquated nomenclature persists", ordinal=0)
    run_id, persisted = _persist_chunks(session, [chunk])
    chunk_p = persisted[0]
    # A non-empty revision that rephrases the distinctive raw term away.
    _contextualize(
        session,
        [chunk_p],
        ["the outdated naming convention persists in this context"],
        chunking_run_id=run_id,
    )

    under_chunk = provider.search("nomenclature", SearchKind.CHUNK)
    under_either = provider.search("nomenclature", SearchKind.EITHER)
    under_ctx = provider.search("nomenclature", SearchKind.CONTEXTUALIZED)

    assert [r.chunk_id for r in under_chunk] == [chunk_p.chunk_id]
    assert under_chunk[0].matched_in_chunk is True
    assert under_chunk[0].matched_in_contextualized is False
    assert [r.chunk_id for r in under_either] == [chunk_p.chunk_id]
    assert under_ctx == []


def test_search_type_filter_self_contained_matches_contextualized_by_raw(
    session: Session,
    provider: Fts5SearchProvider,
) -> None:
    """A self-contained chunk (empty revision) matches the contextualized filter by its raw text."""
    chunk = _make_chunk("bioluminescence in deep-sea organisms", ordinal=0)
    run_id, persisted = _persist_chunks(session, [chunk])
    chunk_p = persisted[0]
    _contextualize(session, [chunk_p], [""], chunking_run_id=run_id)  # empty => self-contained

    under_ctx = provider.search("bioluminescence", SearchKind.CONTEXTUALIZED)
    assert [r.chunk_id for r in under_ctx] == [chunk_p.chunk_id]
    assert under_ctx[0].matched_in_contextualized is True


# --------------------------------------------------------------------------- #
# Ranking — document relevance then span_start
# --------------------------------------------------------------------------- #


def test_search_orders_by_document_relevance_then_span_start(
    session: Session,
    provider: Fts5SearchProvider,
) -> None:
    """A denser document precedes a sparser one; within a document chunks follow span_start.

    Document A has the term in two short chunks (high relative density); document B
    has it once in a longer chunk (lower density). A's chunks must precede B's, and
    A's two chunks must appear in ascending ``span_start`` order — not the
    ``chunk_id`` order the manifest helpers return.
    """
    # Document A: two short, term-dense chunks. The later span_start is given the
    # smaller ordinal so chunk_id order would disagree with document order.
    a_late = _make_chunk("quantum quantum tail", ordinal=0, span_start=900, source_id=_AIZK_UUID)
    a_early = _make_chunk("quantum quantum head", ordinal=1, span_start=10, source_id=_AIZK_UUID)
    _run_a, persisted_a = _persist_chunks(session, [a_late, a_early], source_id=_AIZK_UUID)
    a_late_p, a_early_p = persisted_a

    # Document B: the term once, diluted by a long body (lower bm25 relevance).
    b_text = "quantum " + " ".join(f"filler{i}" for i in range(60))
    b_chunk = _make_chunk(b_text, ordinal=0, span_start=0, source_id=_AIZK_UUID_B)
    _run_b, persisted_b = _persist_chunks(session, [b_chunk], source_id=_AIZK_UUID_B)
    b_chunk_p = persisted_b[0]

    results = provider.search("quantum", SearchKind.CHUNK)

    assert [r.chunk_id for r in results] == [a_early_p.chunk_id, a_late_p.chunk_id, b_chunk_p.chunk_id], (
        "A's chunks precede B's (more relevant document first), and within A the "
        "chunks follow span_start (10 before 900), not chunk_id order"
    )
    assert [r.span_start for r in results[:2]] == [10, 900]


# --------------------------------------------------------------------------- #
# Dedup — aggregate by chunk_id
# --------------------------------------------------------------------------- #


def test_search_both_sides_match_yields_single_result_with_both_flags(
    session: Session,
    provider: Fts5SearchProvider,
) -> None:
    """A term in both raw and contextualized text yields one result with both flags set."""
    chunk = _make_chunk("photosynthesis converts light", ordinal=0)
    run_id, persisted = _persist_chunks(session, [chunk])
    chunk_p = persisted[0]
    _contextualize(
        session,
        [chunk_p],
        ["photosynthesis converts sunlight into chemical energy"],
        chunking_run_id=run_id,
    )

    results = provider.search("photosynthesis", SearchKind.EITHER)
    assert len(results) == 1, "the two matching FTS rows aggregate into one per-chunk result"
    assert results[0].chunk_id == chunk_p.chunk_id
    assert results[0].matched_in_chunk is True
    assert results[0].matched_in_contextualized is True


# --------------------------------------------------------------------------- #
# Input handling — escaping
# --------------------------------------------------------------------------- #


def test_search_empty_query_returns_empty_not_corpus(session: Session, provider: Fts5SearchProvider) -> None:
    """Empty/whitespace input yields no results (not the whole corpus) and does not query."""
    chunk = _make_chunk("some indexed content", ordinal=0)
    _persist_chunks(session, [chunk])

    assert provider.search("") == []
    assert provider.search("   \t ") == []


@pytest.mark.parametrize("raw", ['quartz "loupe" *', "AND OR NEAR", 'star* "x"', "-minus"])
def test_search_query_syntax_characters_are_literal_and_never_error(
    session: Session,
    provider: Fts5SearchProvider,
    raw: str,
) -> None:
    """Input with FTS query-syntax characters is matched literally and never raises."""
    chunk = _make_chunk("ordinary searchable body", ordinal=0)
    _persist_chunks(session, [chunk])

    # No match expected, but the call must complete without raising a query error.
    assert provider.search(raw) == []


def test_search_finds_literal_special_character_term(session: Session, provider: Fts5SearchProvider) -> None:
    """A literal token containing a query-syntax character matches that token in content."""
    chunk = _make_chunk("the c++ language and its quirks", ordinal=0)
    _run_id, persisted = _persist_chunks(session, [chunk])
    chunk_p = persisted[0]

    results = provider.search("c++", SearchKind.CHUNK)
    assert [r.chunk_id for r in results] == [chunk_p.chunk_id]


# --------------------------------------------------------------------------- #
# Memo exclusion — search surface
# --------------------------------------------------------------------------- #


def test_search_excludes_source_without_active_variant_run(
    session: Session,
    provider: Fts5SearchProvider,
) -> None:
    """A source mid-contextualization (chunks only, no active variant run) yields no contextualized hits.

    The chunk is persisted (and so is raw-searchable), but no variant run exists, so
    a contextualized-filter search returns nothing — there is no committed active
    variant run to draw from, and retained intermediate model outputs are never
    indexed.
    """
    chunk = _make_chunk("uncontextualized raw passage", ordinal=0)
    _run_id, persisted = _persist_chunks(session, [chunk])
    chunk_p = persisted[0]

    # Raw side is searchable...
    assert [r.chunk_id for r in provider.search("uncontextualized", SearchKind.CHUNK)] == [chunk_p.chunk_id]
    # ...but the contextualized side has no active variant run, so no contextualized hit.
    assert provider.search("uncontextualized", SearchKind.CONTEXTUALIZED) == []
    # Under either, the result has the contextualized flag unset (no active variant).
    either = provider.search("uncontextualized", SearchKind.EITHER)
    assert [r.chunk_id for r in either] == [chunk_p.chunk_id]
    assert either[0].matched_in_contextualized is False
