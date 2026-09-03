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
inline copy of the DDL and backfill SQL — migrations must stay stable against app
refactors, so they do not import this module, and each migration's copy targets
the schema as of that revision; the ``test_rebuild_reproduces_content`` test
guards that a rebuild and the live inserts agree on today's schema.

The index's ``scope_id`` and ``chunk_id`` columns match what they exist to join:
``pipeline_runs.scope_id`` and ``graph_chunks.chunk_id`` /
``graph_chunk_run_manifest.chunk_id``. All four columns bind through the ORM
``Uuid`` type, so each pair shares one storage form (dashless hex under SQLite):
the live inserts bind a ``uuid.UUID`` through that type, and the rebuild copies
``graph_chunks.source_id`` and ``graph_chunks.chunk_id`` verbatim rather than
rendering them. A diverged value inserts without error and simply matches no run
or no manifest entry, so both write paths reject one first: the rebuild checks the
stored form of the columns it copies, and the live inserts check that the values
handed to them are ``uuid.UUID`` instances.

The FTS ``run_id`` column is load-bearing only for contextualized rows: a
contextualized row's ``run_id`` is the variant run id, used at query time to keep
only the active variant run's rows. A chunk row's ``run_id`` is not query
load-bearing (chunk membership is decided by joining the active chunking-run
manifest on ``chunk_id``); the live insert records the creating chunking run id,
while the backfill/rebuild — where the creating run is not recoverable from the
stored chunk row — leaves it ``NULL``.
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

#: Insert one chunk row into the index. Bound parameters mirror the column order.
_INSERT_CHUNK_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "
    "VALUES (:text, 'chunk', :chunk_id, :run_id, :scope_id)"
)

#: Insert one contextualized row into the index.
_INSERT_CONTEXTUALIZED_SQL = (
    "INSERT INTO graph_content_fts (text, kind, chunk_id, run_id, scope_id) "
    "VALUES (:text, 'contextualized', :chunk_id, :run_id, :scope_id)"
)


def _assert_scope_key_form(scope_id: object) -> None:
    """Reject a scope identity the search join could never match.

    The index's ``scope_id`` binds through the ORM ``Uuid`` type, matching
    ``pipeline_runs.scope_id``. A value that is not a ``uuid.UUID`` instance would
    bind incorrectly (or not at all) and the content would stay committed but
    unfindable. Rejecting at the insert makes the mismatch an error naming the
    value, rather than silence discovered later as missing search results.

    Args:
        scope_id: The scope identity about to be indexed.

    Raises:
        TypeError: If ``scope_id`` is not a ``uuid.UUID`` instance.
    """
    if not isinstance(scope_id, UUID):
        raise TypeError(f"scope_id {scope_id!r} is not a UUID; the content index would match no run")


def _assert_chunk_identity(chunk_id: object) -> None:
    """Reject a chunk identity the manifest join could never match.

    The index's ``chunk_id`` binds through the ORM ``Uuid`` type, matching
    ``graph_chunk_run_manifest.chunk_id``. A value that is not a ``uuid.UUID``
    instance would bind incorrectly (or not at all) and the content would stay
    committed but unjoinable to any manifest entry. Rejecting at the insert makes
    the mismatch an error naming the value, rather than silence discovered later
    as missing search results.

    Args:
        chunk_id: The chunk identity about to be indexed.

    Raises:
        TypeError: If ``chunk_id`` is not a ``uuid.UUID`` instance.
    """
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
    """Append one ``kind='chunk'`` row to the content index in the caller's transaction.

    Called once per chunk-row *creation* in the persist path (a reused ``chunk_id``
    is not re-created, so it is not re-indexed). Does not commit; the caller owns
    the transaction.

    Args:
        session: Active session whose connection carries the open write transaction.
        text_: The raw chunk text to index.
        chunk_id: The surrogate chunk identity.
        run_id: The creating chunking run id (not query load-bearing for chunks).
        scope_id: The chunking run's scope — the source identity the search join
            matches against ``pipeline_runs.scope_id``.

    Raises:
        TypeError: If ``scope_id`` or ``chunk_id`` is not a ``uuid.UUID`` instance.
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
        scope_id: The variant run's scope — the source identity the search join
            matches against ``pipeline_runs.scope_id``.

    Raises:
        TypeError: If ``scope_id`` or ``chunk_id`` is not a ``uuid.UUID`` instance.
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
    """Fail before the backfill copies a stored chunk identity that is not the ``sa.Uuid`` form.

    :data:`_BACKFILL_CHUNKS_SQL` and :data:`_BACKFILL_CONTEXTUALIZED_SQL` copy
    ``graph_chunks.source_id`` and ``graph_chunks.chunk_id`` verbatim into the
    index's ``scope_id`` and ``chunk_id``, so a value of the wrong length or case
    on either column would carry a corrupted identity into the index rather than
    raising. Rejecting first turns that into an error naming the count, at the
    point where the data is still inspectable.

    Args:
        connection: The connection the rebuild statements will run on.

    Raises:
        ValueError: If any ``graph_chunks.source_id`` or ``graph_chunks.chunk_id``
            is not 32 lowercase hex characters.
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
    """Reconstruct ``graph_content_fts`` from the source tables (replay / recovery).

    Clears the index and re-backfills it from every committed ``graph_chunks`` and
    ``graph_contextualized_chunks`` row — the same all-committed-rows backfill the
    creating migration performs — so the index is a faithful, regenerable
    projection of the content rather than only of content written after it existed.
    Runs on the given connection; the caller owns the transaction.

    Args:
        connection: An open connection whose transaction the rebuild runs within.

    Raises:
        ValueError: If any stored chunk identity is not the ``sa.Uuid`` storage
            form the backfill's verbatim copy requires.
    """
    _assert_uuid_storage_form(connection)
    connection.execute(text("DELETE FROM graph_content_fts"))
    connection.execute(text(_BACKFILL_CHUNKS_SQL))
    connection.execute(text(_BACKFILL_CONTEXTUALIZED_SQL))
