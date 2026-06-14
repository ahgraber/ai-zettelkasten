"""Persist splitter-emitted chunks under a source-scoped chunking run.

The splitter stays a pure, I/O-free function; this module is the distinct
component that durably stores its output. :func:`persist_chunks` opens a new
chunking run for a **source** (its durable ``aizk_uuid``, superseding the prior
active run via the shared :func:`aizk.pipeline.run.record_run` primitive), reuses
or inserts each chunk row addressed by its content-derived ``chunk_id`` carrying
**stable identity facts only**, records what the run consumed (the
``conversion_output_id`` locator and ``markdown_hash_xx64``) in an append-only
:class:`~aizk.graph.datamodel.ChunkRunInput`, and records what it produced (each
chunk's ``span``) in an append-only :class:`~aizk.graph.datamodel.ChunkRunManifest`.

Facts are split by what they are about. A chunk row is content-addressed and
shared across every generation that re-emits it, so it carries only facts
invariant for that ``chunk_id``. The generation-varying facts — the source
markdown hash, the splitter version, and each chunk's ``span`` — live on the run
(``ChunkRunInput``) and its manifest (``ChunkRunManifest``). Round-trip fidelity
of the emitted :class:`aizk.chunking.Chunk` is therefore reconstructed by joining
the chunk identity to the run that emitted it; :func:`reconstruct_chunk` and
:func:`chunks_of_run` own that lossless mapping.

Calling convention mirrors the runtime helpers: this module calls
``session.add(...)`` / ``session.flush()`` only and never commits — the caller's
surrounding transaction (a ``BEGIN IMMEDIATE`` block under the single serialized
writer) owns commit boundaries.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import select

from aizk.chunking import Chunk as SplitterChunk
from aizk.graph.content_index import index_chunk_content
from aizk.graph.datamodel import Chunk, ChunkRunInput, ChunkRunManifest, ContextualizationOutputMemo
from aizk.graph.db import begin_immediate
from aizk.pipeline.run import PipelineRun, RunStatus, record_run

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine
    from sqlmodel import Session

    from aizk.graph.datamodel import MemoKind

logger = logging.getLogger(__name__)

#: Stage identifier stamped on chunking runs in ``pipeline_runs``.
CHUNKING_STAGE = "chunking"


def _chunking_derivation_key(markdown_hash_xx64: str, splitter_version: int) -> str:
    """Canonical derivation key for a chunking run.

    Encodes the two inputs that determine the emitted chunk set — the source
    markdown hash and the splitter behavior version — so a content change or a
    ``splitter_version`` bump yields a new key (and a superseding run), while an
    unchanged re-chunk reuses the active run. ``splitter_version`` belongs in the
    derivation key, not only the version stamps, because the same markdown under a
    new splitter version is a different derived dataset.
    """
    return json.dumps(
        {"markdown_hash": markdown_hash_xx64, "splitter_version": splitter_version},
        sort_keys=True,
        separators=(",", ":"),
    )


def _heading_path_to_json(heading_path: tuple[str, ...]) -> str:
    """Serialize a heading path to a canonical, round-trippable JSON array."""
    return json.dumps(list(heading_path), ensure_ascii=False, separators=(",", ":"))


def to_chunk_row(chunk: SplitterChunk) -> Chunk:
    """Map an in-memory splitter chunk to its persisted stable-identity row (no I/O).

    Only the chunk's stable identity facts are carried onto the row; the
    generation-varying facts (``markdown_hash_xx64``, ``splitter_version``,
    ``span``) are recorded against the emitting run, not here.
    """
    return Chunk(
        chunk_id=chunk.chunk_id,
        content_hash=chunk.content_hash,
        doc_id=chunk.doc_id,
        heading_path_json=_heading_path_to_json(chunk.heading_path),
        ordinal=chunk.ordinal,
        text=chunk.text,
        char_count=chunk.char_count,
    )


def _stable_facts(row: Chunk) -> tuple[str, str, str, int, str, int]:
    """Return a chunk row's stable identity facts as a comparable tuple.

    These are the facts that must be invariant for a content-addressed
    ``chunk_id``; comparing them detects an existing row whose identity conflicts
    with an incoming chunk claiming the same id.
    """
    return (row.content_hash, row.doc_id, row.heading_path_json, row.ordinal, row.text, row.char_count)


def reconstruct_chunk(
    row: Chunk,
    *,
    span: tuple[int, int],
    conversion_output_id: str,
    markdown_hash_xx64: str,
    splitter_version: int,
) -> SplitterChunk:
    """Rebuild the in-memory splitter chunk from its identity row and emitting run.

    The inverse of :func:`to_chunk_row` plus the run's generation-varying facts:
    ``span`` comes from the run's manifest entry, ``markdown_hash_xx64`` and the
    source artifact locator from the run's input, and ``splitter_version`` from the
    run. Every field is restored to its emitted value, including ``heading_path``
    (decoded from JSON back to a tuple). The splitter's ``converted_artifact_id``
    is the source-artifact locator, equal to the run's ``conversion_output_id``.
    """
    return SplitterChunk(
        chunk_id=row.chunk_id,
        content_hash=row.content_hash,
        doc_id=row.doc_id,
        heading_path=tuple(json.loads(row.heading_path_json)),
        ordinal=row.ordinal,
        text=row.text,
        char_count=row.char_count,
        converted_artifact_id=conversion_output_id,
        markdown_hash_xx64=markdown_hash_xx64,
        span=span,
        splitter_version=splitter_version,
    )


def persist_chunks(
    session: "Session",
    *,
    aizk_uuid: str,
    conversion_output_id: str,
    markdown_hash_xx64: str,
    splitter_version: int,
    chunks: "Sequence[SplitterChunk]",
) -> PipelineRun:
    """Persist a source's emitted chunks under its active chunking run.

    The run is scoped by the durable source identity ``aizk_uuid`` (not the
    per-conversion artifact id) and keyed by a derivation key over
    ``markdown_hash_xx64`` and ``splitter_version``. If the source's active
    chunking run already carries that derivation key **and** the same manifest set,
    this is a no-op that returns the active run — an accidental rerun with
    unchanged inputs neither opens a new run nor churns rows. Otherwise a new run
    is opened (demoting the prior active run to ``superseded`` in the same
    transaction); the run's consumed Markdown is recorded once in
    :class:`~aizk.graph.datamodel.ChunkRunInput`, and each chunk is persisted by
    its content-addressed ``chunk_id`` (an existing row is reused unmodified, a
    novel one inserted exactly once) with its ``span`` recorded in
    :class:`~aizk.graph.datamodel.ChunkRunManifest`. Prior runs, chunk rows,
    inputs, and manifests are never mutated or deleted, so a ``chunk_id`` shared
    across re-chunks stays a single immutable row made current by its manifest
    entry in the active run.

    Does **not** commit; the caller owns the surrounding transaction.

    Args:
        session: Active session; the caller owns commit/rollback.
        aizk_uuid: The durable source identity (``str(aizk_uuid)``); the run's
            scope and the chunks' ``doc_id``.
        conversion_output_id: Locator for the source Markdown this run consumed;
            recorded as run input provenance (not a derivation input).
        markdown_hash_xx64: Content hash of the source markdown; part of the
            run's derivation key, the re-chunk change signal, and the run input's
            verification fingerprint.
        splitter_version: Behavior version of the splitter that produced the
            chunks; part of the run's derivation key and recorded as a version
            stamp.
        chunks: The chunks emitted by the splitter for this source.

    Returns:
        The active chunking :class:`~aizk.pipeline.run.PipelineRun` — the
        newly-opened run, or the reused prior run when inputs are unchanged.

    Raises:
        ValueError: If any chunk's ``doc_id``, ``converted_artifact_id``,
            ``markdown_hash_xx64``, or ``splitter_version`` does not match the
            run's — guarding against persisting chunks whose provenance disagrees
            with the run keyed by those values; or if a chunk's ``chunk_id``
            already exists with different stable identity facts — guarding the
            content-addressed invariant (every stable fact must be a function of
            ``chunk_id``) against a hash collision or a caller that fabricates a
            colliding id, which would otherwise repoint the manifest at the wrong
            source text.
    """
    mismatched = [
        c.chunk_id
        for c in chunks
        if (c.doc_id, c.converted_artifact_id, c.markdown_hash_xx64, c.splitter_version)
        != (aizk_uuid, conversion_output_id, markdown_hash_xx64, splitter_version)
    ]
    if mismatched:
        raise ValueError(
            f"chunks {mismatched} do not match the run provenance "
            f"(aizk_uuid={aizk_uuid!r}, conversion_output_id={conversion_output_id!r}, "
            f"markdown_hash_xx64={markdown_hash_xx64!r}, splitter_version={splitter_version})"
        )

    # The content-addressed identity must be invariant for a chunk_id: an existing
    # row may be reused only if its stable facts match what this chunk would write.
    conflicting = [
        c.chunk_id
        for c in chunks
        if (existing := session.get(Chunk, c.chunk_id)) is not None
        and _stable_facts(existing) != _stable_facts(to_chunk_row(c))
    ]
    if conflicting:
        raise ValueError(
            f"chunks {conflicting} reuse an existing chunk_id with different stable identity facts "
            "(content-addressed identity must be invariant for a chunk_id)"
        )

    derivation_key = _chunking_derivation_key(markdown_hash_xx64, splitter_version)
    incoming_manifest = {(c.chunk_id, c.span[0], c.span[1]) for c in chunks}
    active = active_chunking_run(session, aizk_uuid)
    if (
        active is not None
        and active.id is not None
        and active.derivation_key == derivation_key
        and {(m.chunk_id, m.span_start, m.span_end) for m in manifest_of_run(session, active.id)} == incoming_manifest
    ):
        logger.debug(
            "Reusing active chunking run id=%s for source=%s (unchanged derivation key and manifest)",
            active.id,
            aizk_uuid,
        )
        return active

    run = record_run(
        session,
        stage=CHUNKING_STAGE,
        scope_key=aizk_uuid,
        derivation_key=derivation_key,
        version_stamps={"splitter_version": str(splitter_version)},
    )
    session.add(
        ChunkRunInput(
            run_id=run.id,
            conversion_output_id=conversion_output_id,
            markdown_hash_xx64=markdown_hash_xx64,
        )
    )

    for chunk in chunks:
        if session.get(Chunk, chunk.chunk_id) is None:
            session.add(to_chunk_row(chunk))
            # Index the raw text once, on chunk-row creation: a reused chunk_id is
            # not re-created here and was already indexed from its first creation.
            index_chunk_content(
                session,
                text_=chunk.text,
                chunk_id=chunk.chunk_id,
                run_id=run.id,
                doc_id=aizk_uuid,
            )
        session.add(
            ChunkRunManifest(
                run_id=run.id,
                chunk_id=chunk.chunk_id,
                span_start=chunk.span[0],
                span_end=chunk.span[1],
            )
        )

    logger.debug(
        "Persisted %d chunks under chunking run id=%s source=%s markdown_hash=%s",
        len(chunks),
        run.id,
        aizk_uuid,
        markdown_hash_xx64,
    )
    return run


def active_chunking_run(session: "Session", aizk_uuid: str) -> PipelineRun | None:
    """Return the active chunking run for a source (``aizk_uuid``), or ``None``."""
    return session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == CHUNKING_STAGE,
            PipelineRun.scope_key == aizk_uuid,
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one_or_none()


def run_input(session: "Session", run_id: int) -> ChunkRunInput | None:
    """Return the recorded input (consumed Markdown locator + hash) for a chunking run."""
    return session.get(ChunkRunInput, run_id)


def manifest_of_run(session: "Session", run_id: int) -> list[ChunkRunManifest]:
    """Return a chunking run's manifest entries, ordered by ``chunk_id``.

    Each entry carries the chunk's ``span`` in that generation's markdown. The
    manifest carries no document-order column; ordering is the (stable,
    deterministic) ``chunk_id`` order. A consumer that needs document order reads
    :attr:`aizk.graph.datamodel.Chunk.ordinal`.
    """
    return list(
        session.exec(
            select(ChunkRunManifest).where(ChunkRunManifest.run_id == run_id).order_by(ChunkRunManifest.chunk_id)
        ).all()
    )


def members_of_run(session: "Session", run_id: int) -> list[str]:
    """Return the ``chunk_id``s in a chunking run's manifest, ordered by ``chunk_id``."""
    return list(
        session.exec(
            select(ChunkRunManifest.chunk_id)
            .where(ChunkRunManifest.run_id == run_id)
            .order_by(ChunkRunManifest.chunk_id)
        ).all()
    )


def chunks_of_run(session: "Session", run_id: int) -> list[SplitterChunk]:
    """Reconstruct a chunking run's emitted chunks by joining identity ⋈ manifest ⋈ input ⋈ run.

    Each chunk is rebuilt field-for-field from its content-addressed identity row,
    its ``span`` from the run's manifest entry, the consumed artifact locator and
    markdown hash from the run's input, and the ``splitter_version`` from the run's
    version stamps. Returned in ``chunk_id`` order (the manifest's order).

    Raises:
        ValueError: If the run, its input, or a manifested chunk identity is
            missing — a corrupt or partially-compacted run cannot be rebuilt.
    """
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise ValueError(f"chunking run {run_id!r} is missing")
    inp = run_input(session, run_id)
    if inp is None:
        raise ValueError(f"chunking run {run_id!r} has no recorded input")
    splitter_version = int(json.loads(run.version_stamps_json)["splitter_version"])

    reconstructed: list[SplitterChunk] = []
    for entry in manifest_of_run(session, run_id):
        row = session.get(Chunk, entry.chunk_id)
        if row is None:
            raise ValueError(f"chunk {entry.chunk_id!r} manifested by run {run_id!r} is missing")
        reconstructed.append(
            reconstruct_chunk(
                row,
                span=(entry.span_start, entry.span_end),
                conversion_output_id=inp.conversion_output_id,
                markdown_hash_xx64=inp.markdown_hash_xx64,
                splitter_version=splitter_version,
            )
        )
    return reconstructed


def current_chunk_ids(session: "Session", aizk_uuid: str) -> set[str]:
    """Return the ``chunk_id``s current for a source: the manifest of its active run.

    A ``chunk_id`` is current if and only if it is in the source's active chunking
    run's manifest; chunks whose only run is superseded are not current, though
    their rows remain present and unmodified.
    """
    run = active_chunking_run(session, aizk_uuid)
    if run is None or run.id is None:
        return set()
    return set(members_of_run(session, run.id))


# --------------------------------------------------------------------------- #
# Contextualization output memo
#
# Internal scratch state caching validated contextualization model outputs so a
# retry of a partially-completed attempt re-invokes the model only for outputs
# not already retained. Identity is ``(kind, scope_key, derivation_key)``; the
# value ``''`` is a legal present-empty entry distinct from absence. These helpers
# are the only access path to ``graph_contextualization_output_memo``; the memo is
# never read as product state.
# --------------------------------------------------------------------------- #


def memo_get(session: "Session", kind: "MemoKind", scope_key: str, derivation_key: str) -> str | None:
    """Return the retained output for a memo key, or ``None`` if absent.

    Distinguishes three cases the caller must treat differently: ``None`` means the
    key is absent (a miss — the model must be invoked); ``''`` means a validated
    present-empty entry (a hit — the chunk was judged already self-contained, so the
    model is not re-invoked); a non-empty string is a retained revision/summary hit.
    """
    row = session.exec(
        select(ContextualizationOutputMemo).where(
            ContextualizationOutputMemo.kind == kind,
            ContextualizationOutputMemo.scope_key == scope_key,
            ContextualizationOutputMemo.derivation_key == derivation_key,
        )
    ).one_or_none()
    return row.output_text if row is not None else None


def memo_upsert_and_read(
    engine: "Engine", kind: "MemoKind", scope_key: str, derivation_key: str, output_text: str
) -> str:
    """Idempotently retain ``output_text`` for a memo key and return the authoritative stored value.

    Opens its own short ``BEGIN IMMEDIATE`` transaction (never spanning a model
    call), inserts the row with ``ON CONFLICT(kind, scope_key, derivation_key) DO
    NOTHING``, then reads and returns the value now stored. On a conflict the insert
    is a no-op and the **pre-existing** value is returned, so a benign same-source
    contention (two work-units re-deriving the same key) resolves to one
    authoritative value: the caller must use this returned value — not its own
    just-generated output — for all downstream derivation and persistence, because
    model output is non-deterministic and a loser's value may differ from the
    winner's.

    Returns:
        The value stored under the key after the upsert (the caller's value when it
        won the insert, or the prior value on conflict).
    """
    statement = (
        sqlite_insert(ContextualizationOutputMemo)
        .values(kind=kind, scope_key=scope_key, derivation_key=derivation_key, output_text=output_text)
        .on_conflict_do_nothing(index_elements=["kind", "scope_key", "derivation_key"])
    )
    with begin_immediate(engine) as session:
        session.execute(statement)
        stored = memo_get(session, kind, scope_key, derivation_key)
    if stored is None:  # pragma: no cover — a row always exists after insert-or-ignore
        raise RuntimeError(f"memo upsert for ({kind!r}, {scope_key!r}) produced no stored row")
    return stored


def memo_delete_keys(session: "Session", scope_key: str, keys: "Sequence[tuple[MemoKind, str]]") -> None:
    """Delete exactly the listed ``(kind, derivation_key)`` memo entries under ``scope_key``.

    Key-exact, not source-wide: it removes only the entries a completed generation
    consumed, leaving any other same-``scope_key`` entry (e.g. a concurrent
    same-source attempt working under different keys) intact. Runs in the caller's
    transaction so the prune commits atomically with the generation's persistence.
    """
    for kind, derivation_key in keys:
        session.execute(
            delete(ContextualizationOutputMemo).where(
                ContextualizationOutputMemo.scope_key == scope_key,
                ContextualizationOutputMemo.kind == kind,
                ContextualizationOutputMemo.derivation_key == derivation_key,
            )
        )
