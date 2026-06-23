"""FTS5 content-index maintenance for graph chunk and contextualized text.

The graph stage keeps a single SQLite FTS5 virtual table,
``graph_content_fts``, indexing both the raw chunk text (``kind='chunk'``) and
each contextualized variant (``kind='contextualized'``) so an operator can search
content with a raw ``MATCH``. The index is **append-only derived state**: rows are
inserted inside the existing write transactions as chunks and variants are
committed, and every committed row is indexed regardless of active/superseded
status — currency is decided at query time, not by the index. Indexing only from
the committed persist path (never the contextualization output memo) structurally
guarantees retained intermediate model outputs are never searchable.

This module owns the table's DDL, the two append-only live-insert helpers the
persist path calls inside its transaction, and the :func:`rebuild_content_index`
routine that reconstructs the index from the source tables for replay or
corruption recovery. The Alembic migration that creates the table carries its own
inline copy of the same DDL and backfill SQL — migrations must stay stable against
app refactors, so they do not import this module; the
``test_rebuild_reproduces_content`` test guards that the two stay equivalent.

The FTS ``run_id`` column is load-bearing only for contextualized rows: a
contextualized row's ``run_id`` is the variant run id, used at query time to keep
only the active variant run's rows. A chunk row's ``run_id`` is not query
load-bearing (chunk membership is decided by joining the active chunking-run
manifest on ``chunk_id``); the live insert records the creating chunking run id,
while the backfill/rebuild — where the creating run is not recoverable from the
content-addressed chunk row — leaves it ``NULL``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Connection
    from sqlmodel import Session

#: DDL creating the discriminated FTS5 content index. ``text`` is the only indexed
#: column; ``kind``/``chunk_id``/``run_id``/``source_id`` are ``UNINDEXED`` so the
#: query can filter and label matches without a side join.
CONTENT_FTS_DDL = (
    "CREATE VIRTUAL TABLE graph_content_fts USING fts5("
    "text, kind UNINDEXED, chunk_id UNINDEXED, run_id UNINDEXED, source_id UNINDEXED)"
)

#: Backfill the chunk side from every committed ``graph_chunks`` row. The creating
#: chunking run is not recoverable from the content-addressed row, so ``run_id`` is
#: ``NULL`` (chunk membership is a manifest join, not an FTS ``run_id`` filter).
_BACKFILL_CHUNKS_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, source_id) "
    "SELECT text, 'chunk', chunk_id, NULL, source_id FROM graph_chunks"
)

#: Backfill the contextualized side from every committed variant. An empty revision
#: indexes the raw chunk text (a self-contained chunk's contextualized
#: representation is its raw text); ``source_id`` and the raw text come from the joined
#: ``graph_chunks`` row. ``run_id`` is the variant run id (query load-bearing).
_BACKFILL_CONTEXTUALIZED_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, source_id) "
    "SELECT CASE WHEN cc.contextualized_text = '' THEN c.text ELSE cc.contextualized_text END, "
    "'contextualized', cc.chunk_id, cc.run_id, c.source_id "
    "FROM graph_contextualized_chunks cc JOIN graph_chunks c ON c.chunk_id = cc.chunk_id"
)

#: Insert one chunk row into the index. Bound parameters mirror the column order.
_INSERT_CHUNK_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, source_id) "
    "VALUES (:text, 'chunk', :chunk_id, :run_id, :source_id)"
)

#: Insert one contextualized row into the index.
_INSERT_CONTEXTUALIZED_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, source_id) "
    "VALUES (:text, 'contextualized', :chunk_id, :run_id, :source_id)"
)


def index_chunk_content(
    session: Session,
    *,
    text_: str,
    chunk_id: str,
    run_id: int | None,
    source_id: str,
) -> None:
    """Append one ``kind='chunk'`` row to the content index in the caller's transaction.

    Called once per chunk-row *creation* in the persist path (a reused ``chunk_id``
    is not re-created, so it is not re-indexed). Does not commit; the caller owns
    the transaction.

    Args:
        session: Active session whose connection carries the open write transaction.
        text_: The raw chunk text to index.
        chunk_id: The content-addressed chunk identity.
        run_id: The creating chunking run id (not query load-bearing for chunks).
        source_id: The source identity (``str(source_id)``).
    """
    session.connection().execute(
        text(_INSERT_CHUNK_SQL),
        {"text": text_, "chunk_id": chunk_id, "run_id": run_id, "source_id": source_id},
    )


def index_contextualized_content(
    session: Session,
    *,
    text_: str,
    chunk_id: str,
    run_id: int | None,
    source_id: str,
) -> None:
    """Append one ``kind='contextualized'`` row to the content index in the caller's transaction.

    Called once per committed variant. The caller passes the raw chunk text when
    the variant's revision is empty (a self-contained chunk's contextualized
    representation is its raw text). Does not commit; the caller owns the
    transaction.

    Args:
        session: Active session whose connection carries the open write transaction.
        text_: The text to index (the revision, or the raw chunk text when empty).
        chunk_id: The source chunk identity.
        run_id: The variant run id (query load-bearing — the active-variant filter).
        source_id: The source identity (``str(source_id)``).
    """
    session.connection().execute(
        text(_INSERT_CONTEXTUALIZED_SQL),
        {"text": text_, "chunk_id": chunk_id, "run_id": run_id, "source_id": source_id},
    )


def rebuild_content_index(connection: Connection) -> None:
    """Reconstruct ``graph_content_fts`` from the source tables (replay / recovery).

    Clears the index and re-backfills it from every committed ``graph_chunks`` and
    ``graph_contextualized_chunks`` row — the same all-committed-rows backfill the
    creating migration performs — so the index is a faithful, regenerable
    projection of the content rather than only of content written after it existed.
    Runs on the given connection; the caller owns the transaction.

    Args:
        connection: An open connection whose transaction the rebuild runs within.
    """
    connection.execute(text("DELETE FROM graph_content_fts"))
    connection.execute(text(_BACKFILL_CHUNKS_SQL))
    connection.execute(text(_BACKFILL_CONTEXTUALIZED_SQL))
