"""Integration tests for the graph operator explorer: document browser + search.

Drives the real graph operator app over a migration-built SQLite database (see the
package ``conftest``), seeding true persisted state through the real
``persist_chunks`` / ``summarize_document`` / ``contextualize_chunks`` paths (with a
deterministic stub LLM and precomputed revisions) so the spine, detail panel, and
paired search results read the same committed records production reads. The
on-demand source-markdown reconstruction is exercised against a fake
:class:`~aizk.graph.markdown_source.BlobReader` and a seeded ``ConversionOutput``,
never real S3.

Asserts observable contracts — spine reading order and chunking facts, the detail
panel's revision-vs-self-contained partitions and provenance, paired highlight on
the matched side(s), select-opens-document, and the memo-exclusion surface (raw
chunks present, no contextualized representation, no retained intermediate revision)
— never model output text or implementation internals.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlmodel import Session
import xxhash

from fastapi.testclient import TestClient

from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk
from aizk.chunking.datamodel import derive_chunk_id
from aizk.conversion.datamodel.job import ConversionJob
from aizk.graph.contextualization import contextualize_chunks, summarize_document
from aizk.graph.datamodel import MEMO_KIND_REVISION
from aizk.graph.llm import StubLLMClient
from aizk.graph.persistence import memo_get, memo_upsert_and_read, persist_chunks

_HASH = "0011223344556677"
_DOC_TEXT = "# Title\n\nThe document body the summary pass reads."


def _make_chunk(
    text_: str,
    *,
    ordinal: int,
    span_start: int,
    aizk_uuid: str,
    conversion_output_id: int,
    heading_path: tuple[str, ...] = (),
    markdown_hash: str = _HASH,
) -> SplitterChunk:
    """Build a content-addressed splitter chunk with an explicit ``span_start``."""
    content_hash = xxhash.xxh64(text_.encode("utf-8")).hexdigest()
    chunk_id = derive_chunk_id(aizk_uuid, heading_path, ordinal, content_hash)
    return SplitterChunk(
        chunk_id=chunk_id,
        content_hash=content_hash,
        doc_id=aizk_uuid,
        heading_path=heading_path,
        ordinal=ordinal,
        text=text_,
        char_count=len(text_),
        converted_artifact_id=str(conversion_output_id),
        markdown_hash_xx64=markdown_hash,
        span=(span_start, span_start + len(text_)),
        splitter_version=SPLITTER_VERSION,
    )


def _seed_chunks(
    db_session: Session,
    chunks: list[SplitterChunk],
    *,
    aizk_uuid: str,
    conversion_output_id: int,
    markdown_hash: str = _HASH,
) -> int:
    """Persist a chunk set via the real persist path and return its chunking run id."""
    run = persist_chunks(
        db_session,
        aizk_uuid=aizk_uuid,
        conversion_output_id=str(conversion_output_id),
        markdown_hash_xx64=markdown_hash,
        splitter_version=SPLITTER_VERSION,
        chunks=chunks,
    )
    db_session.commit()
    assert run.id is not None
    return run.id


def _seed_contextualization(
    db_session: Session,
    chunks: list[SplitterChunk],
    revisions: Sequence[str],
    *,
    aizk_uuid: str,
    conversion_output_id: int,
    chunking_run_id: int,
    markdown_hash: str = _HASH,
) -> None:
    """Summarize + contextualize the chunks with canned revisions via the real path; commit."""
    summary = summarize_document(
        db_session,
        StubLLMClient(),
        aizk_uuid=aizk_uuid,
        conversion_output_id=str(conversion_output_id),
        markdown_hash_xx64=markdown_hash,
        document_text=_DOC_TEXT,
    )
    contextualize_chunks(
        db_session,
        StubLLMClient(),
        aizk_uuid=aizk_uuid,
        summary=summary,
        chunks=chunks,
        chunking_run_id=chunking_run_id,
        splitter_version=SPLITTER_VERSION,
        precomputed_revisions=list(revisions),
    )
    db_session.commit()


def _seed_source_and_output(db_session: Session, seed_source, seed_conversion_output, *, karakeep_id: str, title: str):
    """Seed a source, its conversion job, and a conversion output; return ``(aizk_uuid_str, conversion_output_id)``.

    The migration-built schema enforces the ``conversion_outputs.job_id`` →
    ``conversion_jobs.id`` foreign key, so a job is seeded before the output.
    """
    source = seed_source(db_session, karakeep_id=karakeep_id, title=title)
    job = ConversionJob(
        aizk_uuid=source.aizk_uuid,
        owner_id="self",
        title=title,
        idempotency_key=f"idem:{karakeep_id}",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    output = seed_conversion_output(
        db_session, job_id=job.id, aizk_uuid=source.aizk_uuid, title=title, markdown_hash_xx64=_HASH
    )
    return str(source.aizk_uuid), output.id


# --------------------------------------------------------------------------- #
# Document browser spine — span_start order + chunking facts
# --------------------------------------------------------------------------- #


def test_explorer_spine_lists_chunks_in_span_start_order_with_facts(
    explorer_client: TestClient, db_session, seed_source, seed_conversion_output
) -> None:
    """The spine lists active chunks in span_start order with heading path, span, char count, and the self-contained marker."""
    doc_id, output_id = _seed_source_and_output(
        db_session, seed_source, seed_conversion_output, karakeep_id="bm_spine", title="Spine Doc"
    )
    # The later span_start is given the smaller ordinal so chunk_id order would
    # disagree with reading order — the spine must follow span_start, not chunk_id.
    late = _make_chunk(
        "the later passage in the document",
        ordinal=0,
        span_start=900,
        aizk_uuid=doc_id,
        conversion_output_id=output_id,
        heading_path=("Results",),
    )
    early = _make_chunk(
        "the earlier introductory passage",
        ordinal=1,
        span_start=10,
        aizk_uuid=doc_id,
        conversion_output_id=output_id,
        heading_path=("Intro",),
    )
    run_id = _seed_chunks(db_session, [late, early], aizk_uuid=doc_id, conversion_output_id=output_id)
    # early is a non-empty revision; late is self-contained (empty revision).
    _seed_contextualization(
        db_session,
        [late, early],
        ["", "the earlier introductory passage, fully spelled out"],
        aizk_uuid=doc_id,
        conversion_output_id=output_id,
        chunking_run_id=run_id,
    )

    response = explorer_client.get("/ui/graph/explorer", params={"doc_id": doc_id})

    assert response.status_code == 200
    body = response.text
    # Reading order: the early chunk (span_start 10) precedes the late one (span_start 900).
    assert body.index(early.chunk_id) < body.index(late.chunk_id)
    # Heading path, span, and char count are surfaced.
    assert "Intro" in body and "Results" in body
    assert "span 10–" in body
    assert "span 900–" in body
    assert f"{early.char_count} chars" in body
    # The self-contained chunk (late, empty revision) is marked self-contained.
    assert 'data-self-contained="true"' in body
    assert 'data-self-contained="false"' in body


# --------------------------------------------------------------------------- #
# Detail panel — revision vs self-contained, both with provenance
# --------------------------------------------------------------------------- #


def test_explorer_detail_shows_revision_distinct_from_raw_with_provenance(
    explorer_client: TestClient, db_session, seed_source, seed_conversion_output
) -> None:
    """A chunk with a non-empty revision shows the revision distinct from raw, with provenance and markdown."""
    doc_id, output_id = _seed_source_and_output(
        db_session, seed_source, seed_conversion_output, karakeep_id="bm_rev", title="Revision Doc"
    )
    chunk = _make_chunk(
        "it improves throughput", ordinal=0, span_start=0, aizk_uuid=doc_id, conversion_output_id=output_id
    )
    run_id = _seed_chunks(db_session, [chunk], aizk_uuid=doc_id, conversion_output_id=output_id)
    revision = "scaled dot-product attention improves throughput"
    _seed_contextualization(
        db_session, [chunk], [revision], aizk_uuid=doc_id, conversion_output_id=output_id, chunking_run_id=run_id
    )

    response = explorer_client.get("/ui/graph/explorer", params={"doc_id": doc_id, "chunk_id": chunk.chunk_id})

    assert response.status_code == 200
    body = response.text
    # The contextualized representation is the revision, shown distinct from the raw chunk.
    assert revision in body
    assert "it improves throughput" in body
    assert 'data-self-contained="false"' in body
    # Provenance: variant run with version + model_profile, plus summary + chunking lineage.
    assert "variant run #" in body
    assert "context_version" in body
    assert "model_profile" in body
    assert "summary run #" in body
    assert "chunking run #" in body
    # On-demand source markdown reconstructed via the fake blob reader.
    assert "reconstructed source markdown body" in body


def test_explorer_detail_self_contained_shows_raw_marked_with_same_provenance(
    explorer_client: TestClient, db_session, seed_source, seed_conversion_output
) -> None:
    """A self-contained chunk (empty revision) shows the raw text marked self-contained with the same provenance lineage."""
    doc_id, output_id = _seed_source_and_output(
        db_session, seed_source, seed_conversion_output, karakeep_id="bm_sc", title="Self Doc"
    )
    chunk = _make_chunk(
        "bioluminescence in deep-sea organisms",
        ordinal=0,
        span_start=0,
        aizk_uuid=doc_id,
        conversion_output_id=output_id,
    )
    run_id = _seed_chunks(db_session, [chunk], aizk_uuid=doc_id, conversion_output_id=output_id)
    _seed_contextualization(
        db_session, [chunk], [""], aizk_uuid=doc_id, conversion_output_id=output_id, chunking_run_id=run_id
    )

    response = explorer_client.get("/ui/graph/explorer", params={"doc_id": doc_id, "chunk_id": chunk.chunk_id})

    assert response.status_code == 200
    body = response.text
    # The consumed representation is the raw chunk text, marked self-contained.
    assert "bioluminescence in deep-sea organisms" in body
    assert 'data-self-contained="true"' in body
    assert "revision empty" in body
    # Same provenance lineage as a revised chunk.
    assert "variant run #" in body
    assert "summary run #" in body
    assert "chunking run #" in body


# --------------------------------------------------------------------------- #
# Select-opens-document
# --------------------------------------------------------------------------- #


def test_explorer_select_result_opens_document_at_chunk_with_contextualized_detail(
    explorer_client: TestClient, db_session, seed_source, seed_conversion_output
) -> None:
    """Selecting a search result opens its document at the chunk with the detail showing its contextualized representation.

    A search row's selection is an ``hx-get`` to the document-browser route carrying
    ``doc_id`` + ``chunk_id``; following that request must open the document at the
    chunk with the detail panel populated by its contextualized representation.
    """
    doc_id, output_id = _seed_source_and_output(
        db_session, seed_source, seed_conversion_output, karakeep_id="bm_select", title="Select Doc"
    )
    chunk = _make_chunk(
        "it improves throughput", ordinal=0, span_start=0, aizk_uuid=doc_id, conversion_output_id=output_id
    )
    run_id = _seed_chunks(db_session, [chunk], aizk_uuid=doc_id, conversion_output_id=output_id)
    revision = "scaled dot-product attention improves throughput"
    _seed_contextualization(
        db_session, [chunk], [revision], aizk_uuid=doc_id, conversion_output_id=output_id, chunking_run_id=run_id
    )

    # The search row exposes the select target as an hx-get to /ui/graph/explorer.
    search = explorer_client.post(
        "/ui/graph/explorer/search", data={"query": "scaled dot-product attention", "kind": "either"}
    )
    assert search.status_code == 200
    assert "/ui/graph/explorer?doc_id=" in search.text
    assert chunk.chunk_id in search.text

    # Following the selection (HX-Request partial) opens the document at the chunk
    # with the detail panel showing the contextualized representation.
    opened = explorer_client.get(
        "/ui/graph/explorer",
        params={"doc_id": doc_id, "chunk_id": chunk.chunk_id},
        headers={"HX-Request": "true"},
    )
    assert opened.status_code == 200
    body = opened.text
    assert 'class="spine-chunk selected"' in body or "selected" in body
    assert revision in body
    assert "variant run #" in body


# --------------------------------------------------------------------------- #
# Paired results highlight
# --------------------------------------------------------------------------- #


def test_explorer_search_highlights_contextualized_only_and_both_sides(
    explorer_client: TestClient, db_session, seed_source, seed_conversion_output
) -> None:
    """A contextualized-only match is marked on the contextualized side only; a both-sides match is one row marked on both."""
    doc_id, output_id = _seed_source_and_output(
        db_session, seed_source, seed_conversion_output, karakeep_id="bm_hl", title="Highlight Doc"
    )
    # ctx_only: the raw says "it"; the revision introduces the searched term.
    ctx_only = _make_chunk(
        "it improves throughput", ordinal=0, span_start=0, aizk_uuid=doc_id, conversion_output_id=output_id
    )
    # both: the term is present in both raw and revision.
    both = _make_chunk(
        "photosynthesis converts light", ordinal=1, span_start=500, aizk_uuid=doc_id, conversion_output_id=output_id
    )
    run_id = _seed_chunks(db_session, [ctx_only, both], aizk_uuid=doc_id, conversion_output_id=output_id)
    _seed_contextualization(
        db_session,
        [ctx_only, both],
        [
            "photosynthesis improves throughput",
            "photosynthesis converts sunlight into chemical energy",
        ],
        aizk_uuid=doc_id,
        conversion_output_id=output_id,
        chunking_run_id=run_id,
    )

    response = explorer_client.post("/ui/graph/explorer/search", data={"query": "photosynthesis", "kind": "either"})

    assert response.status_code == 200
    body = response.text
    # One row per chunk: two matching chunks → two rows (not four).
    assert body.count('class="result-row"') == 2
    # The contextualized-only chunk matched only on the contextualized side.
    assert f'data-chunk-id="{ctx_only.chunk_id}"' in body
    assert "match: contextualized only" in body
    # The both-sides chunk is a single row marked on both.
    assert f'data-chunk-id="{both.chunk_id}"' in body
    assert "match: both" in body
    # The term is wrapped in <mark> in the rendered results.
    assert "<mark>photosynthesis</mark>" in body or "<mark>Photosynthesis</mark>" in body


def test_explorer_search_contextualized_only_not_marked_in_raw(
    explorer_client: TestClient, db_session, seed_source, seed_conversion_output
) -> None:
    """For a contextualized-only match, the raw side carries no highlight mark."""
    doc_id, output_id = _seed_source_and_output(
        db_session, seed_source, seed_conversion_output, karakeep_id="bm_hl2", title="Highlight Doc 2"
    )
    chunk = _make_chunk(
        "it improves throughput", ordinal=0, span_start=0, aizk_uuid=doc_id, conversion_output_id=output_id
    )
    run_id = _seed_chunks(db_session, [chunk], aizk_uuid=doc_id, conversion_output_id=output_id)
    _seed_contextualization(
        db_session,
        [chunk],
        ["scaled dot-product attention improves throughput"],
        aizk_uuid=doc_id,
        conversion_output_id=output_id,
        chunking_run_id=run_id,
    )

    response = explorer_client.post("/ui/graph/explorer/search", data={"query": "scaled", "kind": "either"})

    assert response.status_code == 200
    body = response.text
    raw_block = body.split('class="result-raw"', 1)[1].split('class="result-contextualized"', 1)[0]
    ctx_block = body.split('class="result-contextualized"', 1)[1]
    # The mark appears only on the contextualized side.
    assert "<mark>" not in raw_block
    assert "<mark>scaled</mark>" in ctx_block


# --------------------------------------------------------------------------- #
# Search input handled safely at the explorer route boundary
# --------------------------------------------------------------------------- #


def test_explorer_search_empty_query_renders_empty_partial(
    explorer_client: TestClient, db_session, seed_source, seed_conversion_output
) -> None:
    """An empty or whitespace-only query renders the empty-results partial (zero rows), not the whole corpus.

    Seeds real searchable content so a leak would surface as result rows; the empty
    and whitespace-only queries must each return 200 with no ``result-row`` rather
    than every seeded chunk.
    """
    doc_id, output_id = _seed_source_and_output(
        db_session, seed_source, seed_conversion_output, karakeep_id="bm_empty", title="Empty Query Doc"
    )
    chunk = _make_chunk(
        "a searchable passage that must not leak",
        ordinal=0,
        span_start=0,
        aizk_uuid=doc_id,
        conversion_output_id=output_id,
    )
    _seed_chunks(db_session, [chunk], aizk_uuid=doc_id, conversion_output_id=output_id)

    empty = explorer_client.post("/ui/graph/explorer/search", data={"query": "", "kind": "either"})
    whitespace = explorer_client.post("/ui/graph/explorer/search", data={"query": "   \t  ", "kind": "either"})

    # Both yield the empty-results partial: 200, zero rows, and the seeded corpus is not dumped.
    assert empty.status_code == 200
    assert 'class="result-row"' not in empty.text
    assert chunk.chunk_id not in empty.text
    assert whitespace.status_code == 200
    assert 'class="result-row"' not in whitespace.text
    assert chunk.chunk_id not in whitespace.text


def test_explorer_search_syntax_characters_do_not_error(
    explorer_client: TestClient, db_session, seed_source, seed_conversion_output
) -> None:
    """FTS5-syntax characters in a query never error the page; a literal match is returned, a non-match is empty.

    Operator input is treated as literal terms, so a query carrying ``"``, ``*``, or
    a boolean-operator word like ``AND`` must return 200 (never a 500). A literal
    match against seeded content is returned; a non-matching special-char query
    returns the empty-results partial.
    """
    doc_id, output_id = _seed_source_and_output(
        db_session, seed_source, seed_conversion_output, karakeep_id="bm_syntax", title="Syntax Doc"
    )
    # The chunk literally contains the FTS5 operator characters so a literal-term
    # query for them matches rather than being interpreted as query syntax.
    chunk = _make_chunk(
        'wildcard* and "quoted" tokens', ordinal=0, span_start=0, aizk_uuid=doc_id, conversion_output_id=output_id
    )
    _seed_chunks(db_session, [chunk], aizk_uuid=doc_id, conversion_output_id=output_id)

    literal = explorer_client.post("/ui/graph/explorer/search", data={"query": 'wildcard* "quoted"', "kind": "either"})
    boolean_word = explorer_client.post("/ui/graph/explorer/search", data={"query": "AND OR NEAR", "kind": "either"})

    # Special-char query never errors the page and returns the literal match.
    assert literal.status_code == 200
    assert f'data-chunk-id="{chunk.chunk_id}"' in literal.text
    # A boolean-operator query is matched literally (no such tokens in the corpus):
    # 200 with the empty-results partial, never a 500 from interpreted FTS syntax.
    assert boolean_word.status_code == 200
    assert 'class="result-row"' not in boolean_word.text


# --------------------------------------------------------------------------- #
# Memo exclusion — explorer surface
# --------------------------------------------------------------------------- #


def test_explorer_source_mid_contextualization_shows_raw_no_representation_no_memo(
    explorer_client: TestClient, db_session, seed_source, seed_conversion_output
) -> None:
    """A source mid-contextualization shows raw chunks but no contextualized representation, and no retained revision appears.

    The chunk is persisted (raw-searchable, present in the spine), but no active
    variant run exists; a retained intermediate revision in the memo must not appear
    in the spine, the detail panel, or search.
    """
    doc_id, output_id = _seed_source_and_output(
        db_session, seed_source, seed_conversion_output, karakeep_id="bm_memo", title="Memo Doc"
    )
    chunk = _make_chunk(
        "uncontextualized raw passage", ordinal=0, span_start=0, aizk_uuid=doc_id, conversion_output_id=output_id
    )
    _seed_chunks(db_session, [chunk], aizk_uuid=doc_id, conversion_output_id=output_id)

    # Retain an intermediate revision in the memo (scratch state for an in-progress
    # attempt) — it must never surface anywhere in the explorer.
    retained = "RETAINED-INTERMEDIATE-floccinaucinihilipilification"
    memo_upsert_and_read(db_session.get_bind(), MEMO_KIND_REVISION, doc_id, "some-derivation-key", retained)

    # Spine: the raw chunk is present, marked as having no contextualized representation.
    spine = explorer_client.get("/ui/graph/explorer", params={"doc_id": doc_id})
    assert spine.status_code == 200
    assert chunk.chunk_id in spine.text
    assert "no contextualized representation" in spine.text
    assert "no active variant run" in spine.text
    assert retained not in spine.text

    # Detail: raw text shown, no variant; the retained revision does not appear.
    detail = explorer_client.get("/ui/graph/explorer", params={"doc_id": doc_id, "chunk_id": chunk.chunk_id})
    assert detail.status_code == 200
    assert "uncontextualized raw passage" in detail.text
    assert 'data-has-variant="false"' in detail.text
    assert retained not in detail.text

    # Search: the raw side is searchable; the retained revision is not.
    raw_hit = explorer_client.post("/ui/graph/explorer/search", data={"query": "uncontextualized", "kind": "either"})
    assert raw_hit.status_code == 200
    assert chunk.chunk_id in raw_hit.text
    memo_hit = explorer_client.post(
        "/ui/graph/explorer/search", data={"query": "floccinaucinihilipilification", "kind": "either"}
    )
    assert memo_hit.status_code == 200
    assert retained not in memo_hit.text
    assert 'class="result-row"' not in memo_hit.text

    # Confirm the memo row truly exists (the exclusion is real, not a seeding no-op).
    assert memo_get(db_session, MEMO_KIND_REVISION, doc_id, "some-derivation-key") == retained
