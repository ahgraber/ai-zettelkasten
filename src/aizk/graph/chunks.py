"""Persist splitter-emitted chunks under a source-scoped chunking run.

The splitter stays a pure, I/O-free function; this module is the distinct
component that durably stores its output and assigns each chunk its identity.
:func:`persist_chunks` opens a new chunking run for a **source** (its durable
``source_id``, superseding the prior active run via the shared
:func:`aizk.pipeline.run.record_run` primitive), reuses the existing surrogate
``chunk_id`` for a chunk whose sameness-key ``(source_id, heading_path, ordinal,
content_hash)`` is already present and mints a new surrogate otherwise — each row
carrying **stable identity facts only** — records what the run consumed (the
``conversion_output_id`` locator, ``markdown_hash_xx64``, and the emitted chunk
set's output fingerprint) in an append-only :class:`~aizk.graph.datamodel.ChunkRunInput`,
and records what it produced (each chunk's ``span``) in an append-only
:class:`~aizk.graph.datamodel.ChunkRunManifest`. When the unchanged-rerun fast
path reuses the active run for a different ``conversion_output_id``, it also
records a :class:`~aizk.graph.datamodel.ChunkRunApplicability` row. It returns
the persisted chunks carrying their assigned surrogate so the caller can
contextualize them without re-reading the manifest.

Facts are split by what they are about. A chunk row is identified by a stable
surrogate and shared across every generation that re-emits it, so it carries only
facts invariant for that ``chunk_id``. The generation-varying facts — the source
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
from uuid import UUID, uuid4

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import select

from aizk.chunking import Chunk as SplitterChunk
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.db.engine import begin_immediate
from aizk.graph.content_index import index_chunk_content
from aizk.graph.datamodel import (
    INPUT_KIND_RAW,
    Chunk,
    ChunkRunApplicability,
    ChunkRunInput,
    ChunkRunManifest,
    ContextualizationOutputMemo,
)
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.pipeline.identity import DERIVATION_KEY_FORMAT, derivation_key
from aizk.pipeline.run import PipelineRun, active_run, record_applicability, record_run, require_active_run
from aizk.utilities.hashing import content_fingerprint, fingerprint

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine
    from sqlmodel import Session

    from aizk.graph.datamodel import MemoKind

logger = logging.getLogger(__name__)

#: Stage identifier stamped on chunking runs in ``pipeline_runs``.
CHUNKING_STAGE = "chunking"


def chunking_input_fingerprint(markdown_fingerprint: str) -> str:
    """Return the fingerprint for a chunking run's source Markdown.

    ``markdown_fingerprint`` is the source Markdown's content fingerprint.
    """
    return fingerprint({"markdown_fingerprint": markdown_fingerprint})


def chunking_config_fingerprint(splitter_version: int) -> str:
    """Return a chunking run's config fingerprint: the splitter behavior version."""
    return fingerprint({"splitter_version": splitter_version})


def chunking_derivation_key(markdown_fingerprint: str, splitter_version: int) -> str:
    """Return the derivation key for a chunking run.

    The key depends on the source Markdown fingerprint and ``splitter_version``.
    """
    return derivation_key(
        inputs={
            "input": chunking_input_fingerprint(markdown_fingerprint),
            "config": chunking_config_fingerprint(splitter_version),
        }
    )


def _heading_path_to_json(heading_path: tuple[str, ...]) -> str:
    """Serialize a heading path to a canonical, round-trippable JSON array."""
    return json.dumps(list(heading_path), ensure_ascii=False, separators=(",", ":"))


def to_chunk_row(chunk: SplitterChunk, *, chunk_id: UUID) -> Chunk:
    """Map a splitter chunk to a stable-identity row without I/O.

    The returned row contains the chunk's stable identity fields and the supplied
    ``chunk_id``.

    Raises:
        ValueError: If ``chunk.source_id`` is not a UUID.
    """
    return Chunk(
        chunk_id=chunk_id,
        content_hash=chunk.content_hash,
        source_id=UUID(chunk.source_id),
        heading_path_json=_heading_path_to_json(chunk.heading_path),
        ordinal=chunk.ordinal,
        text=chunk.text,
        char_count=chunk.char_count,
    )


def _stable_facts(row: Chunk) -> tuple[str, str, str, int, str, int]:
    """Return a chunk row's stable identity facts as a comparable tuple.

    The tuple includes content hash, source, heading path, ordinal, text, and
    character count.
    """
    return (row.content_hash, str(row.source_id), row.heading_path_json, row.ordinal, row.text, row.char_count)


def _incoming_facts(chunk: SplitterChunk) -> tuple[str, str, str, int, str, int]:
    """Return an incoming splitter chunk's stable identity facts, comparable to :func:`_stable_facts`."""
    return (
        chunk.content_hash,
        chunk.source_id,
        _heading_path_to_json(chunk.heading_path),
        chunk.ordinal,
        chunk.text,
        chunk.char_count,
    )


def _chunk_by_sameness_key(session: "Session", chunk: SplitterChunk) -> Chunk | None:
    """Return the persisted chunk matching ``chunk``'s sameness key, or ``None``.

    Raises:
        ValueError: If ``chunk.source_id`` is not a UUID.
    """
    return session.exec(
        select(Chunk).where(
            Chunk.source_id == UUID(chunk.source_id),
            Chunk.heading_path_json == _heading_path_to_json(chunk.heading_path),
            Chunk.ordinal == chunk.ordinal,
            Chunk.content_hash == chunk.content_hash,
        )
    ).one_or_none()


def reconstruct_chunk(
    row: Chunk,
    *,
    span: tuple[int, int],
    conversion_output_id: str,
    markdown_hash_xx64: str,
    splitter_version: int,
) -> SplitterChunk:
    """Reconstruct the emitted splitter chunk from its row and run facts.

    Restores the chunk identity, heading path, span, artifact locator, Markdown
    hash, and splitter version.
    """
    return SplitterChunk(
        chunk_id=str(row.chunk_id),
        content_hash=row.content_hash,
        source_id=str(row.source_id),
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
    source_id: UUID,
    conversion_output_id: str,
    markdown_hash_xx64: str,
    markdown_fingerprint: str,
    splitter_version: int,
    chunks: "Sequence[SplitterChunk]",
) -> tuple[PipelineRun, list[SplitterChunk]]:
    """Persist emitted chunks under the active source-scoped chunking run.

    An unchanged derivation key and manifest reuses the active run; otherwise a
    new run supersedes it. Each chunk reuses the surrogate for its sameness key or
    receives a new one. Prior rows remain unchanged, and a different conversion
    locator records an applicability relation.

    The function stages the run, input, manifest, and new chunk rows but does not
    commit. Returned chunks keep the supplied document order and carry their IDs.

    Raises:
        ValueError: If chunk provenance is inconsistent or a sameness-key match
            has conflicting stable identity facts.
    """
    mismatched = [
        c.content_hash
        for c in chunks
        if (c.source_id, c.converted_artifact_id, c.markdown_hash_xx64, c.splitter_version)
        != (str(source_id), conversion_output_id, markdown_hash_xx64, splitter_version)
    ]
    if mismatched:
        raise ValueError(
            f"chunks with content_hash {mismatched} do not match the run provenance "
            f"(source_id={source_id!r}, conversion_output_id={conversion_output_id!r}, "
            f"markdown_hash_xx64={markdown_hash_xx64!r}, splitter_version={splitter_version})"
        )

    # Resolve each chunk to its existing surrogate by sameness-key (None when novel).
    # A sameness-key match whose remaining stable facts differ is a content_hash
    # collision: reusing the row would repoint the manifest at the wrong text.
    existing_rows = [_chunk_by_sameness_key(session, c) for c in chunks]
    conflicting = [
        c.content_hash
        for row, c in zip(existing_rows, chunks, strict=True)
        if row is not None and _stable_facts(row) != _incoming_facts(c)
    ]
    if conflicting:
        raise ValueError(
            f"chunks with content_hash {conflicting} reuse an existing sameness-key with different "
            "stable identity facts (a content_hash collision)"
        )

    run_derivation_key = chunking_derivation_key(markdown_fingerprint, splitter_version)
    active = active_chunking_run(session, source_id)
    # Unchanged-rerun fast path: reuse the active run only when every chunk already
    # exists (so it carries a surrogate) and the active manifest is exactly this
    # (surrogate, span) set. A novel chunk means the content changed, so it falls
    # through to a superseding run.
    if (
        active is not None
        and active.id is not None
        and active.derivation_key == run_derivation_key
        and all(row is not None for row in existing_rows)
    ):
        incoming_manifest = {
            (row.chunk_id, c.span[0], c.span[1]) for row, c in zip(existing_rows, chunks, strict=True)
        }
        if {(m.chunk_id, m.span_start, m.span_end) for m in manifest_of_run(session, active.id)} == incoming_manifest:
            logger.debug(
                "Reusing active chunking run id=%s for source=%s (unchanged derivation key and manifest)",
                active.id,
                source_id,
            )
            active_input = run_input(session, active.id)
            if active_input is not None and active_input.conversion_output_id != conversion_output_id:
                # The active run is also current for a later artifact locator whose
                # content is unchanged. The writer is idempotent on the primary key.
                record_chunk_run_applicability(
                    session,
                    run_id=active.id,
                    conversion_output_id=conversion_output_id,
                    input_fingerprint=chunking_input_fingerprint(markdown_fingerprint),
                    splitter_version=splitter_version,
                )
            return active, [
                c.model_copy(update={"chunk_id": str(row.chunk_id)})
                for row, c in zip(existing_rows, chunks, strict=True)
            ]

    run = record_run(
        session,
        stage=CHUNKING_STAGE,
        scope_id=source_id,
        derivation_key=run_derivation_key,
        version_stamps={
            "splitter_version": str(splitter_version),
            "input_fingerprint": chunking_input_fingerprint(markdown_fingerprint),
            "config_fingerprint": chunking_config_fingerprint(splitter_version),
            "derivation_key_format": DERIVATION_KEY_FORMAT,
        },
    )
    chunk_set_fingerprint = payload_fingerprint(
        [PayloadItem(chunk_key(c), INPUT_KIND_RAW, content_fingerprint(c.text)) for c in chunks]
    )
    session.add(
        ChunkRunInput(
            run_id=run.id,
            conversion_output_id=conversion_output_id,
            markdown_hash_xx64=markdown_hash_xx64,
            chunk_set_fingerprint=chunk_set_fingerprint,
        )
    )

    persisted: list[SplitterChunk] = []
    for row, chunk in zip(existing_rows, chunks, strict=True):
        if row is None:
            # Mint a stable surrogate for this novel sameness-key and index its raw
            # text once, on creation; a reused chunk is not re-created or re-indexed.
            chunk_id = uuid4()
            session.add(to_chunk_row(chunk, chunk_id=chunk_id))
            index_chunk_content(
                session,
                text_=chunk.text,
                chunk_id=chunk_id,
                run_id=run.id,
                scope_id=source_id,
            )
        else:
            chunk_id = row.chunk_id
        session.add(
            ChunkRunManifest(
                run_id=run.id,
                chunk_id=chunk_id,
                span_start=chunk.span[0],
                span_end=chunk.span[1],
            )
        )
        persisted.append(chunk.model_copy(update={"chunk_id": str(chunk_id)}))

    logger.debug(
        "Persisted %d chunks under chunking run id=%s source=%s markdown_hash=%s",
        len(chunks),
        run.id,
        source_id,
        markdown_hash_xx64,
    )
    return run, persisted


def active_chunking_run(session: "Session", source_id: UUID) -> PipelineRun | None:
    """Return the active chunking run for a source (``source_id``), or ``None``."""
    return active_run(session, CHUNKING_STAGE, source_id)


def run_input(session: "Session", run_id: int) -> ChunkRunInput | None:
    """Return the recorded input (consumed Markdown locator + hash) for a chunking run."""
    return session.get(ChunkRunInput, run_id)


def _conversion_output(session: "Session", conversion_output_id: str) -> ConversionOutput | None:
    """Resolve a stringified conversion-output locator to its row, or ``None`` if absent or unparsable."""
    try:
        native_id = int(conversion_output_id)
    except ValueError:
        return None
    return session.get(ConversionOutput, native_id)


def record_chunk_run_applicability(
    session: "Session",
    *,
    run_id: int,
    conversion_output_id: str,
    input_fingerprint: str,
    splitter_version: int,
) -> ChunkRunApplicability:
    """Validate and stage a chunk-run applicability relation; the caller commits.

    The relation is append-only and idempotent on ``(run_id, conversion_output_id)``.
    Existing rows are returned unchanged.

    Raises:
        ValueError: If the run, conversion output, fingerprints, manifest, or
            existing relation is inconsistent.
    """
    run = require_active_run(session, run_id, CHUNKING_STAGE, label="chunking run")

    output = _conversion_output(session, conversion_output_id)
    if output is None or output.source_id != run.scope_id:
        raise ValueError(
            f"conversion output {conversion_output_id!r} does not exist or does not belong to source {run.scope_id!r}"
        )

    candidate_key = derivation_key(
        inputs={"input": input_fingerprint, "config": chunking_config_fingerprint(splitter_version)}
    )
    if candidate_key != run.derivation_key:
        raise ValueError(f"chunking run {run_id!r}'s derivation key does not match the recomputed candidate key")

    stored_input = run_input(session, run_id)
    if stored_input is None:
        raise ValueError(f"chunking run {run_id!r} has no recorded input (a torn run)")
    if len(manifest_of_run(session, run_id)) == 0 and stored_input.chunk_set_fingerprint != payload_fingerprint([]):
        raise ValueError(f"chunking run {run_id!r} has an empty manifest but a non-empty recorded chunk set")
    manifest_fingerprint = payload_fingerprint(
        [
            PayloadItem(chunk_key(chunk), INPUT_KIND_RAW, content_fingerprint(chunk.text))
            for chunk, _, _ in document_order_chunks(session, run_id)
        ]
    )
    if manifest_fingerprint != stored_input.chunk_set_fingerprint:
        raise ValueError(f"chunking run {run_id!r}'s manifest does not match its recorded chunk set")

    return record_applicability(
        session,
        ChunkRunApplicability,
        {"run_id": run_id, "conversion_output_id": conversion_output_id},
        input_fingerprint=input_fingerprint,
    )


def manifest_of_run(session: "Session", run_id: int) -> list[ChunkRunManifest]:
    """Return a run's manifest entries ordered by ``chunk_id``.

    Each entry includes its generation-specific span. Use
    :func:`document_order_chunks` when reading order matters.
    """
    return list(
        session.exec(
            select(ChunkRunManifest).where(ChunkRunManifest.run_id == run_id).order_by(ChunkRunManifest.chunk_id)
        ).all()
    )


def document_order_chunks(session: "Session", run_id: int) -> list[tuple[Chunk, int, int]]:
    """Return a run's chunks with spans in document order.

    Use this reader when reading order matters.

    Returns:
        ``(chunk_row, span_start, span_end)`` triples ordered by ``span_start``;
        empty when the run has no manifest entries.
    """
    rows = session.exec(
        select(Chunk, ChunkRunManifest.span_start, ChunkRunManifest.span_end)
        .join(ChunkRunManifest, ChunkRunManifest.chunk_id == Chunk.chunk_id)
        .where(ChunkRunManifest.run_id == run_id)
        .order_by(ChunkRunManifest.span_start)
    ).all()
    return [(chunk, span_start, span_end) for chunk, span_start, span_end in rows]


def members_of_run(session: "Session", run_id: int) -> list[UUID]:
    """Return the ``chunk_id``s in a chunking run's manifest, ordered by ``chunk_id``."""
    return list(
        session.exec(
            select(ChunkRunManifest.chunk_id)
            .where(ChunkRunManifest.run_id == run_id)
            .order_by(ChunkRunManifest.chunk_id)
        ).all()
    )


def chunks_of_run(session: "Session", run_id: int) -> list[SplitterChunk]:
    """Reconstruct emitted splitter chunks from a run.

    Restores identity, span, input locator and hash, and splitter version. Returns
    chunks in ``chunk_id`` order.

    Raises:
        ValueError: If the run, input, or manifested chunk identity is missing.
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


def current_chunk_ids(session: "Session", source_id: UUID) -> set[UUID]:
    """Return chunk IDs in a source's active run manifest.

    Superseded-only chunks are excluded while their rows remain stored.
    """
    run = active_chunking_run(session, source_id)
    if run is None or run.id is None:
        return set()
    return set(members_of_run(session, run.id))


# Contextualization output memo
#
# Internal scratch state caching validated contextualization model outputs so a
# retry of a partially-completed attempt re-invokes the model only for outputs
# not already retained. Identity is ``(kind, scope_id, derivation_key)``; the
# value ``''`` is a legal present-empty entry distinct from absence. These helpers
# are the only access path to ``graph_contextualization_output_memo``; the memo is
# never read as product state.


def memo_get(session: "Session", kind: "MemoKind", scope_id: UUID, derivation_key: str) -> str | None:
    """Return memo output for a key, or ``None`` when absent.

    An empty string is a present value and therefore a memo hit.
    """
    row = session.exec(
        select(ContextualizationOutputMemo).where(
            ContextualizationOutputMemo.kind == kind,
            ContextualizationOutputMemo.scope_id == scope_id,
            ContextualizationOutputMemo.derivation_key == derivation_key,
        )
    ).one_or_none()
    return row.output_text if row is not None else None


def memo_upsert_and_read(
    engine: "Engine", kind: "MemoKind", scope_id: UUID, derivation_key: str, output_text: str
) -> str:
    """Store output for a memo key and return the authoritative stored value.

    The function opens a short transaction and returns the existing value on
    conflict. Callers must use this returned value because model output is not
    deterministic.
    """
    statement = (
        sqlite_insert(ContextualizationOutputMemo)
        .values(kind=kind, scope_id=scope_id, derivation_key=derivation_key, output_text=output_text)
        .on_conflict_do_nothing(index_elements=["kind", "scope_id", "derivation_key"])
    )
    with begin_immediate(engine) as session:
        session.execute(statement)
        stored = memo_get(session, kind, scope_id, derivation_key)
    if stored is None:  # pragma: no cover — a row always exists after insert-or-ignore
        raise RuntimeError(f"memo upsert for ({kind!r}, {scope_id!r}) produced no stored row")
    return stored


def memo_delete_keys(session: "Session", scope_id: UUID, keys: "Sequence[tuple[MemoKind, str]]") -> None:
    """Delete the listed memo keys for a scope in the caller's transaction.

    Other keys under the scope remain unchanged.
    """
    for kind, key in keys:
        session.execute(
            delete(ContextualizationOutputMemo).where(
                ContextualizationOutputMemo.scope_id == scope_id,
                ContextualizationOutputMemo.kind == kind,
                ContextualizationOutputMemo.derivation_key == key,
            )
        )
