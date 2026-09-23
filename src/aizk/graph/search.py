"""Provide ranked search over active graph content.

:class:`SearchProvider` accepts literal operator terms and returns per-chunk
results ordered by relevance and document position. :class:`Fts5SearchProvider`
reads the append-only content index without writing. The provider excludes
superseded generations and retained intermediate output.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import Uuid, text

from aizk.graph.chunks import CHUNKING_STAGE
from aizk.graph.variants import VARIANT_STAGE

if TYPE_CHECKING:
    from sqlalchemy import Engine

#: Maximum accepted operator query length, in characters. Input longer than this
#: is truncated (never rejected) so malformed or oversized input cannot fail the
#: page; aligned with the conversion operator UI's search bound.
MAX_QUERY_LENGTH = 200


class SearchKind(str, Enum):
    """Which content side a search matches.

    ``CHUNK`` matches raw text, ``CONTEXTUALIZED`` matches the contextualized
    representation, and ``EITHER`` matches both.
    """

    CHUNK = "chunk"
    CONTEXTUALIZED = "contextualized"
    EITHER = "either"


@dataclass(frozen=True)
class SearchResult:
    """Ranked search hit for one chunk.

    ``score`` is the best matching-row score; lower scores rank higher. Match flags
    identify whether raw or contextualized content matched. Display text and
    highlighting are resolved by the caller.
    """

    source_id: UUID
    chunk_id: UUID
    span_start: int
    score: float
    matched_in_chunk: bool
    matched_in_contextualized: bool


@runtime_checkable
class SearchProvider(Protocol):
    """Search active-generation content and return ranked per-chunk results.

    Implementations treat input as literal terms, order results by document
    relevance then document order, and return an empty list for unusable or
    unmatched input.
    """

    def search(self, query: str, kind: SearchKind = SearchKind.EITHER) -> list[SearchResult]:
        """Return results for ``query`` filtered by ``kind``.

        Results are ordered by document relevance, then ``span_start``. Empty or
        whitespace-only queries return an empty list.
        """
        ...


def escape_fts_query(query: str) -> str | None:
    """Convert operator input to a literal-term FTS5 ``MATCH`` string.

    Truncates input to ``MAX_QUERY_LENGTH`` and quotes each whitespace token,
    including internal quotes. Returns ``None`` for empty or whitespace-only input.
    Callers must bind the result as a query parameter.
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
    # The index's ``scope_id`` and ``chunk_id`` bind through the same ORM ``Uuid``
    # type as ``pipeline_runs.scope_id`` and ``graph_chunk_run_manifest.chunk_id``,
    # which is what makes both joins below legal; ``scope_id`` is aliased back to
    # ``source_id`` because that is what a result means to a caller, and both are
    # read back through ``Uuid`` so the caller gets ``uuid.UUID`` values.
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
    """Search active-generation content with SQLite FTS5.

    Each query uses a short read-only connection and SQLite ``bm25()`` ranking.
    """

    def __init__(self, engine: "Engine") -> None:
        """Store the shared database engine."""
        self._engine = engine

    def search(self, query: str, kind: SearchKind = SearchKind.EITHER) -> list[SearchResult]:
        """Return active-generation results for ``query`` and ``kind``.

        Empty or whitespace-only input returns no results. Results contain one row per
        chunk, ordered by document score and then ``span_start``.
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
                text(_SEARCH_SQL).columns(chunk_id=Uuid(), source_id=Uuid()),
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
    """Aggregate FTS rows into one ranked result per chunk.

    Scores use the best matching row; chunks are ordered by document score and
    ``span_start``.
    """
    by_chunk: dict[UUID, SearchResult] = {}
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

    doc_score: dict[UUID, float] = {}
    for result in by_chunk.values():
        best = doc_score.get(result.source_id)
        if best is None or result.score < best:
            doc_score[result.source_id] = result.score

    return sorted(
        by_chunk.values(),
        key=lambda r: (doc_score[r.source_id], r.source_id, r.span_start),
    )
