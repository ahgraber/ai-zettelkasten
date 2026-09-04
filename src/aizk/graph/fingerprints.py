"""The cross-stage output-payload fingerprint: what a stage produced or consumed.

Chunking, contextualization, and extraction each fingerprint the ordered sequence
of chunk-shaped text a run produced (or, for extraction, read), so a downstream
stage compares its planned payload against the upstream run's declared output
without re-reading either. Every stage calls :func:`payload_fingerprint` over its
own :class:`PayloadItem` sequence, and :func:`chunk_key` computes each item's
portable key from either the in-memory splitter shape or the persisted graph row.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NamedTuple

from aizk.chunking import Chunk as SplitterChunk
from aizk.chunking.datamodel import derive_chunk_content_key
from aizk.graph.datamodel import Chunk as GraphChunk
from aizk.pipeline.identity import fingerprint

if TYPE_CHECKING:
    from collections.abc import Sequence


class PayloadItem(NamedTuple):
    """One chunk-shaped unit of a stage's fingerprinted payload, in document order.

    Attributes:
        chunk_key: Which chunk, by portable content key (see :func:`chunk_key`).
        input_kind: Which representation of it: ``INPUT_KIND_RAW`` or
            ``INPUT_KIND_CONTEXTUALIZED`` (:mod:`aizk.graph.datamodel`).
        text_fingerprint: ``content_fingerprint`` of the text a consumer reads
            for this item.
    """

    chunk_key: str
    input_kind: str
    text_fingerprint: str


def payload_fingerprint(items: "Sequence[PayloadItem]") -> str:
    """Return the order-sensitive fingerprint of a stage's payload.

    Equal fingerprints mean identical chunk keys, input kinds, and text
    fingerprints in the same order; a reordering changes the result.
    """
    return fingerprint([item._asdict() for item in items])


def chunk_key(chunk: "SplitterChunk | GraphChunk") -> str:
    """Return a chunk's portable content key, from either its in-memory or persisted shape.

    A function of ``(source_id, heading_path, ordinal, content_hash)`` only, so
    the splitter's :class:`~aizk.chunking.Chunk` and the
    :class:`~aizk.graph.datamodel.Chunk` row it was persisted as yield the same
    key, and the same logical chunk keys the same on any backend.
    """
    if isinstance(chunk, GraphChunk):
        return derive_chunk_content_key(
            str(chunk.source_id),
            tuple(json.loads(chunk.heading_path_json)),
            chunk.ordinal,
            chunk.content_hash,
        )
    return derive_chunk_content_key(chunk.source_id, chunk.heading_path, chunk.ordinal, chunk.content_hash)
