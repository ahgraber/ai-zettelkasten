"""Data model and content-key derivation for chunking.

Defines the immutable :class:`Chunk` contract emitted by the splitter and
:func:`derive_chunk_content_key`, the deterministic, cross-process-stable chunk
**sameness-key fingerprint**. The content key is a function of the chunk's address
``(source_id, heading_path, ordinal)`` and its ``content_hash`` so that content
edits and address moves are independently observable downstream.

The content key is **not** the chunk's identity: ``chunk_id`` is a stable
surrogate assigned once at persistence (reused across generations when the
sameness-key matches), so the splitter does not produce it. The content key is the
portable fingerprint persistence reuses identities by, and that downstream
derivation keys embed in place of the surrogate so they stay backend-portable.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field
import xxhash


class Chunk(BaseModel):
    """An immutable structural chunk carved from a converted Markdown artifact.

    Attributes:
        chunk_id: The persistence-assigned stable surrogate identity, or ``None``
            on a freshly split chunk. The splitter does not assign it; persistence
            mints a surrogate for a novel sameness-key and reuses the existing one
            otherwise, and a reconstructed chunk carries that surrogate. The
            chunk's portable sameness-key fingerprint is :func:`derive_chunk_content_key`.
        content_hash: xxh64 digest of the chunk's normalized ``text``.
        source_id: Caller-supplied logical document identifier.
        heading_path: Heading texts from outermost to innermost; ``()`` is the
            document root.
        ordinal: Position of this chunk among chunks sharing ``heading_path``,
            counting from 0 in document order.
        text: Chunk content exactly as carved from the source artifact (before
            hash-time normalization).
        char_count: Length of ``text`` in characters.
        converted_artifact_id: Caller-supplied identifier of the source artifact.
        markdown_hash_xx64: Caller-supplied content hash of the source artifact.
        span: ``(start, end)`` character offsets locating ``text`` in the source
            (Python-slice semantics: inclusive start, exclusive end).
        splitter_version: Behavior version of the splitter that produced this chunk.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str | None = Field(
        default=None, description="Persistence-assigned surrogate identity; None until persisted."
    )
    content_hash: str = Field(description="xxh64 digest of the normalized text.")
    source_id: str = Field(description="Logical document identifier.")
    heading_path: tuple[str, ...] = Field(description="Heading texts outermost to innermost; () is the document root.")
    ordinal: int = Field(description="Position among chunks sharing heading_path, from 0.")
    text: str = Field(description="Chunk content as carved from the source artifact.")
    char_count: int = Field(description="Length of text in characters.")
    converted_artifact_id: str = Field(description="Identifier of the source artifact.")
    markdown_hash_xx64: str = Field(description="Content hash of the source artifact.")
    span: tuple[int, int] = Field(description="(start, end) character offsets in the source.")
    splitter_version: int = Field(description="Splitter behavior version.")


def derive_chunk_content_key(
    source_id: str,
    heading_path: tuple[str, ...],
    ordinal: int,
    content_hash: str,
) -> str:
    """Derive a chunk's portable sameness-key fingerprint from its address and content hash.

    The four inputs are serialized to a compact canonical JSON array and hashed
    with xxh64. The serialization is fixed (field order, separators, and
    ``ensure_ascii=False``) so the result is stable across processes; any change
    to it requires bumping ``SPLITTER_VERSION``.

    This is the chunk's **content key**, not its identity: it is the matching
    fingerprint persistence reuses surrogate ``chunk_id`` identities by, and the
    portable value downstream derivation keys embed in place of the database-local
    surrogate so they recompute identically on any backend.

    Args:
        source_id: Logical document identifier.
        heading_path: Heading texts outermost to innermost.
        ordinal: Position among chunks sharing ``heading_path``.
        content_hash: xxh64 digest of the chunk's normalized text.

    Returns:
        Hex-encoded xxHash64 digest (16 characters).
    """
    canonical = json.dumps(
        [source_id, list(heading_path), ordinal, content_hash],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return xxhash.xxh64(canonical.encode("utf-8")).hexdigest()
