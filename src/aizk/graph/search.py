"""Ranked, type-filtered content search over the FTS5 content index.

This module is the **query** side of the graph content index whose write side is
:mod:`aizk.graph.content_index`. It exposes a :class:`SearchProvider` protocol —
an operator query plus a content-type filter mapped to ranked, per-chunk results —
and one FTS5/``bm25`` implementation (:class:`Fts5SearchProvider`, which opens its own
read-only connection per query and never writes). The explorer
depends on the protocol, not the concrete provider, so the relevance backend
(``bm25`` today, a vector model later) is a swap with no caller change.

A search returns only **active-generation** content. The index holds every
committed chunk and variant row regardless of supersession (currency is decided at
query time), so the provider filters each match against the source's currently
active runs:

- a ``kind='chunk'`` match counts only if its ``chunk_id`` is in the source's
  **active chunking-run manifest** — that manifest row also carries the
  ``span_start`` that anchors the result in document order;
- a ``kind='contextualized'`` match counts only if its FTS ``run_id`` equals the
  source's **active variant run** id, which is also what excludes a source still
  mid-contextualization (no active variant run) and any retained intermediate
  model output.

A chunk has up to two matching FTS rows (its raw text and its contextualized
representation), so matches are aggregated by ``chunk_id`` into a single
:class:`SearchResult` carrying per-side flags (``matched_in_chunk`` /
``matched_in_contextualized``); a self-contained chunk's contextualized
representation is its raw text, so it can match on the contextualized side by its
raw text. Each chunk's score is the best (minimum) ``bm25()`` over its matching
rows; a document's score is the best (minimum) over its matching chunks. Results
are ordered by document score ascending (SQLite ``bm25()`` is lower-is-better),
and within a document by ``span_start`` ascending (true reading order, not the
``chunk_id`` order the manifest helpers return).

Operator input is treated as **literal search terms**, never as the FTS query
language: each whitespace token is wrapped in double quotes (internal quotes
doubled) so ``"``, ``*``, ``-``, and ``AND``/``OR``/``NEAR`` are matched
literally, the built string is always passed as a bound parameter, empty or
whitespace-only input short-circuits to no results without querying, and input is
truncated to a bounded length so malformed input can never fail the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sqlalchemy import text

from aizk.graph.contextualization import VARIANT_STAGE
from aizk.graph.persistence import CHUNKING_STAGE

if TYPE_CHECKING:
    from sqlalchemy import Engine

#: Maximum accepted operator query length, in characters. Input longer than this
#: is truncated (never rejected) so malformed or oversized input cannot fail the
#: page; aligned with the conversion operator UI's search bound.
MAX_QUERY_LENGTH = 200


class SearchKind(str, Enum):
    """Which side(s) of a chunk a search matches against.

    ``CHUNK`` restricts matches to the raw chunk text, ``CONTEXTUALIZED`` to the
    contextualized representation (the stored revision, or the raw chunk text when
    the revision is empty), and ``EITHER`` matches both.
    """

    CHUNK = "chunk"
    CONTEXTUALIZED = "contextualized"
    EITHER = "either"


@dataclass(frozen=True)
class SearchResult:
    """One ranked, per-chunk search hit.

    Carries identifiers, ordering keys, and per-side match flags only — never
    rendered text or highlighting. A consumer resolves the raw and contextualized
    display text and marks the query terms from these flags, keeping the provider
    seam thin for a relevance-backend swap.

    Attributes:
        source_id: The source identity (``str(source_id)``) the chunk belongs to.
        chunk_id: The surrogate chunk identity.
        span_start: The chunk's start offset in the active generation's markdown;
            the within-document ordering key.
        score: The chunk's relevance score (best ``bm25()`` over its matching
            rows); lower is more relevant.
        matched_in_chunk: Whether the query matched the raw chunk text.
        matched_in_contextualized: Whether the query matched the contextualized
            representation.
    """

    source_id: str
    chunk_id: str
    span_start: int
    score: float
    matched_in_chunk: bool
    matched_in_contextualized: bool


@runtime_checkable
class SearchProvider(Protocol):
    """A ranked, type-filtered content search over active-generation content.

    Implementations map an operator query and a :class:`SearchKind` filter to a
    list of :class:`SearchResult`, ordered by document relevance then document
    order. Treating input safely at the boundary (literal terms, empty short-
    circuit, bounded length) is part of the contract, so every implementation
    accepts arbitrary operator input without raising.
    """

    def search(self, query: str, kind: SearchKind = SearchKind.EITHER) -> list[SearchResult]:
        """Return ranked per-chunk results for ``query`` under the ``kind`` filter.

        Args:
            query: Raw operator input, treated as literal search terms.
            kind: Which side(s) of a chunk to match against.

        Returns:
            Per-chunk results ordered by document score ascending, then by
            ``span_start`` ascending within a document. Empty when the query has
            no usable terms or nothing matches.
        """
        ...


def escape_fts_query(query: str) -> str | None:
    """Escape operator input into a literal-term FTS5 ``MATCH`` string.

    Truncates to :data:`MAX_QUERY_LENGTH`, splits on whitespace, and wraps each
    token in double quotes (doubling any internal double quote) so every FTS5
    operator character — ``"``, ``*``, ``-``, and the ``AND``/``OR``/``NEAR``
    keywords — is matched literally rather than interpreted. The quoted tokens are
    joined by spaces (implicit ``AND``). The returned string is intended to be
    passed as a bound parameter, never interpolated.

    Args:
        query: Raw operator input.

    Returns:
        The literal-term ``MATCH`` string, or ``None`` when the input has no
        usable terms (empty or whitespace-only) and the search must short-circuit
        to no results.
    """
    tokens = query[:MAX_QUERY_LENGTH].split()
    if not tokens:
        return None
    return " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


#: Fetch matching FTS rows with their per-row ``bm25()`` and the active-generation
#: spine. The chunk side joins the source's active chunking-run manifest (chunk
#: membership + ``span_start``); the contextualized side additionally requires the
#: FTS ``run_id`` to equal the source's active variant run. A contextualized hit
#: with no active-manifest spine row is dropped (the inner manifest join), so every
#: returned row has a ``span_start`` to order by. ``kind`` is bound to honor the
#: type filter ('chunk', 'contextualized', or both via the OR).
_SEARCH_SQL = (
    # The index's ``scope_id`` is the source identity in the same dashed form
    # ``pipeline_runs.scope_id`` holds, which is what makes both joins below legal;
    # it is aliased back to ``source_id`` because that is what a result means to a
    # caller.
    "SELECT f.kind, f.chunk_id, f.scope_id AS source_id, m.span_start, bm25(graph_content_fts) AS score "
    "FROM graph_content_fts AS f "
    "JOIN pipeline_runs AS cr "
    "  ON cr.stage = :chunking_stage AND cr.scope_id = f.scope_id AND cr.status = 'active' "
    # Anchoring on the active chunking-run manifest is an intentional currency
    # decision, not merely a way to obtain a span_start. It deliberately drops a
    # kind='contextualized' hit whose chunk_id is absent from the *current* active
    # chunking manifest. The reachable window: a re-chunk supersedes the chunking
    # run while the prior variant run is still active (cross-stage supersession
    # does not cascade), so that variant's contextualized chunks belong to the now-
    # superseded chunking manifest. Those chunk_ids no longer exist in the active
    # generation, so a result for them would reference a span absent from the active
    # document; dropping them keeps search and the explorer spine on the same active
    # reading-order skeleton.
    "JOIN graph_chunk_run_manifest AS m "
    "  ON m.run_id = cr.id AND m.chunk_id = f.chunk_id "
    "LEFT JOIN pipeline_runs AS vr "
    "  ON vr.stage = :variant_stage AND vr.scope_id = f.scope_id AND vr.status = 'active' "
    "WHERE graph_content_fts MATCH :match "
    "  AND ( "
    "    (f.kind = 'chunk' AND :want_chunk = 1) "
    "    OR (f.kind = 'contextualized' AND :want_contextualized = 1 AND f.run_id = vr.id) "
    "  )"
)


class Fts5SearchProvider:
    """A :class:`SearchProvider` backed by the SQLite FTS5 ``graph_content_fts`` index.

    Holds the shared engine and opens its own short, read-only (AUTOCOMMIT)
    connection per query; it never writes or commits. Matching is ranked by SQLite
    ``bm25()`` (lower is more relevant) and filtered to active-generation content at
    query time.
    """

    def __init__(self, engine: "Engine") -> None:
        """Store the engine the provider opens a read-only connection against per query.

        Args:
            engine: The shared engine; each :meth:`search` opens its own connection.
        """
        self._engine = engine

    def search(self, query: str, kind: SearchKind = SearchKind.EITHER) -> list[SearchResult]:
        """Return ranked per-chunk results for ``query`` under the ``kind`` filter.

        Escapes the input to literal terms (short-circuiting empty/whitespace-only
        input to no results), runs the ``MATCH`` with the active-generation filters
        and per-row ``bm25()``, then aggregates by ``chunk_id`` into one result per
        chunk (per-side flags, best-score) and orders documents by their best score
        ascending and chunks within a document by ``span_start`` ascending.
        """
        match = escape_fts_query(query)
        if match is None:
            return []

        want_chunk = 1 if kind in (SearchKind.CHUNK, SearchKind.EITHER) else 0
        want_contextualized = 1 if kind in (SearchKind.CONTEXTUALIZED, SearchKind.EITHER) else 0

        # Read-only: run in AUTOCOMMIT so the query never issues a BEGIN (and so
        # never takes SQLite's write lock under the single-writer / BEGIN IMMEDIATE
        # convention), keeping search non-blocking alongside the writer.
        with self._engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            rows = connection.execute(
                text(_SEARCH_SQL),
                {
                    "chunking_stage": CHUNKING_STAGE,
                    "variant_stage": VARIANT_STAGE,
                    "match": match,
                    "want_chunk": want_chunk,
                    "want_contextualized": want_contextualized,
                },
            ).all()

        return _aggregate(rows)


def _aggregate(rows: list) -> list[SearchResult]:  # noqa: ANN001 - DBAPI Row sequence
    """Aggregate matching FTS rows by ``chunk_id`` into ranked per-chunk results.

    A chunk has up to two matching rows (raw and contextualized); they collapse
    into one result carrying both per-side flags, scored by the best (minimum)
    ``bm25()`` across them. Documents are ordered by their best chunk score
    ascending (lower is more relevant); within a document, chunks are ordered by
    ``span_start`` ascending. Stable, deterministic ordering for equal scores comes
    from sorting on ``(score, source_id)`` for documents and ``span_start`` for chunks.
    """
    by_chunk: dict[str, SearchResult] = {}
    for kind_value, chunk_id, source_id, span_start, score in rows:
        prior = by_chunk.get(chunk_id)
        matched_in_chunk = kind_value == "chunk"
        if prior is None:
            by_chunk[chunk_id] = SearchResult(
                source_id=source_id,
                chunk_id=chunk_id,
                span_start=span_start,
                score=score,
                matched_in_chunk=matched_in_chunk,
                matched_in_contextualized=not matched_in_chunk,
            )
        else:
            by_chunk[chunk_id] = SearchResult(
                source_id=source_id,
                chunk_id=chunk_id,
                span_start=span_start,
                score=min(prior.score, score),
                matched_in_chunk=prior.matched_in_chunk or matched_in_chunk,
                matched_in_contextualized=prior.matched_in_contextualized or not matched_in_chunk,
            )

    doc_score: dict[str, float] = {}
    for result in by_chunk.values():
        best = doc_score.get(result.source_id)
        if best is None or result.score < best:
            doc_score[result.source_id] = result.score

    return sorted(
        by_chunk.values(),
        key=lambda r: (doc_score[r.source_id], r.source_id, r.span_start),
    )
