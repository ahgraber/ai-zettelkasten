"""Maintain the append-only FTS5 projection of graph content.

The index stores committed raw chunks and contextualized variants. Queries
choose active-generation rows, while :func:`rebuild_content_index` reconstructs
the projection from source tables. Live inserts run in the caller's transaction;
rebuilds use the caller's connection and transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Uuid, bindparam, text

if TYPE_CHECKING:
    from sqlalchemy import Connection
    from sqlmodel import Session

#: Match exactly 32 lowercase hex characters — the form ``sa.Uuid`` stores under
#: SQLite. Bound as a parameter, never interpolated.
_UUID_STORAGE_GLOB = "[0-9a-f]" * 32

#: DDL creating the discriminated FTS5 content index. ``text`` is the only indexed
#: column; ``kind``/``chunk_id``/``run_id``/``scope_id`` are ``UNINDEXED`` so the
#: query can filter and label matches without a side join.
CONTENT_FTS_DDL = (
    "CREATE VIRTUAL TABLE graph_content_fts USING fts5("
    "text, kind UNINDEXED, chunk_id UNINDEXED, run_id UNINDEXED, scope_id UNINDEXED)"
)

#: Backfill the chunk side from every committed ``graph_chunks`` row. ``scope_id``
#: and ``chunk_id`` copy ``c.source_id`` and ``c.chunk_id`` verbatim: all four
#: columns bind through the ORM ``Uuid`` type, so each pair already shares one
#: storage form. The creating chunking run is not recoverable from the stored
#: chunk row, so ``run_id`` is ``NULL`` (chunk membership is a manifest join, not
#: an FTS ``run_id`` filter).
_BACKFILL_CHUNKS_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "  # noqa: S608 — fragment is a constant
    "SELECT c.text, 'chunk', c.chunk_id, NULL, c.source_id FROM graph_chunks c"
)

#: Backfill the contextualized side from every committed variant. An empty revision
#: indexes the raw chunk text (a self-contained chunk's contextualized
#: representation is its raw text); the scope id and the raw text come from the
#: joined ``graph_chunks`` row. ``run_id`` is the variant run id (query load-bearing).
_BACKFILL_CONTEXTUALIZED_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "  # noqa: S608 — fragment is a constant
    "SELECT CASE WHEN cc.contextualized_text = '' THEN c.text ELSE cc.contextualized_text END, "
    "'contextualized', cc.chunk_id, cc.run_id, c.source_id "
    "FROM graph_contextualized_chunks cc JOIN graph_chunks c ON c.chunk_id = cc.chunk_id"
)

_INSERT_CHUNK_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "
    "VALUES (:text, 'chunk', :chunk_id, :run_id, :scope_id)"
)

_INSERT_CONTEXTUALIZED_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "
    "VALUES (:text, 'contextualized', :chunk_id, :run_id, :scope_id)"
)


def _assert_scope_key_form(scope_id: object) -> None:
    """Reject a non-UUID scope identity before indexing."""
    if not isinstance(scope_id, UUID):
        raise TypeError(f"scope_id {scope_id!r} is not a UUID; the content index would match no run")


def _assert_chunk_identity(chunk_id: object) -> None:
    """Reject a non-UUID chunk identity before indexing."""
    if not isinstance(chunk_id, UUID):
        raise TypeError(f"chunk_id {chunk_id!r} is not a UUID; the content index would match no manifest entry")


def index_chunk_content(
    session: Session,
    *,
    text_: str,
    chunk_id: UUID,
    run_id: int | None,
    scope_id: UUID,
) -> None:
    """Stage a raw chunk in the caller's content-index transaction.

    Does not commit. ``scope_id`` and ``chunk_id`` must be UUID instances.
    """
    _assert_scope_key_form(scope_id)
    _assert_chunk_identity(chunk_id)
    session.connection().execute(
        text(_INSERT_CHUNK_SQL).bindparams(bindparam("scope_id", type_=Uuid()), bindparam("chunk_id", type_=Uuid())),
        {"text": text_, "chunk_id": chunk_id, "run_id": run_id, "scope_id": scope_id},
    )


def index_contextualized_content(
    session: Session,
    *,
    text_: str,
    chunk_id: UUID,
    run_id: int | None,
    scope_id: UUID,
) -> None:
    """Stage a contextualized row in the caller's content-index transaction.

    Does not commit. ``scope_id`` and ``chunk_id`` must be UUID instances.
    """
    _assert_scope_key_form(scope_id)
    _assert_chunk_identity(chunk_id)
    session.connection().execute(
        text(_INSERT_CONTEXTUALIZED_SQL).bindparams(
            bindparam("scope_id", type_=Uuid()), bindparam("chunk_id", type_=Uuid())
        ),
        {"text": text_, "chunk_id": chunk_id, "run_id": run_id, "scope_id": scope_id},
    )


def _assert_uuid_storage_form(connection: Connection) -> None:
    """Validate stored chunk identities before rebuilding the content index.

    Raises:
        ValueError: If a source or chunk identity is not 32 lowercase hex characters.
    """
    offenders = connection.execute(
        text("SELECT count(*) FROM graph_chunks WHERE source_id NOT GLOB :storage_form"),
        {"storage_form": _UUID_STORAGE_GLOB},
    ).scalar_one()
    if offenders:
        raise ValueError(
            f"{offenders} graph_chunks row(s) hold a source_id that is not 32 lowercase hex characters; "
            "the content index cannot copy a scope id from them"
        )
    offenders = connection.execute(
        text("SELECT count(*) FROM graph_chunks WHERE chunk_id NOT GLOB :storage_form"),
        {"storage_form": _UUID_STORAGE_GLOB},
    ).scalar_one()
    if offenders:
        raise ValueError(
            f"{offenders} graph_chunks row(s) hold a chunk_id that is not 32 lowercase hex characters; "
            "the content index cannot copy a chunk id from them"
        )


def rebuild_content_index(connection: Connection) -> None:
    """Rebuild the content index from committed chunk and variant rows.

    Runs on ``connection`` without committing. Raises ``ValueError`` for invalid
    stored UUID forms.
    """
    _assert_uuid_storage_form(connection)
    connection.execute(text("DELETE FROM graph_content_fts"))
    connection.execute(text(_BACKFILL_CHUNKS_SQL))
    connection.execute(text(_BACKFILL_CONTEXTUALIZED_SQL))
