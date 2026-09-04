"""Tests for the cross-stage payload fingerprint (:mod:`aizk.graph.fingerprints`).

Covers :func:`payload_fingerprint`'s order- and content-sensitivity — the
property every consuming stage's output-sensitive derivation key relies on — and
:func:`chunk_key`'s agreement between the splitter's in-memory
:class:`~aizk.chunking.Chunk` and its persisted :class:`~aizk.graph.datamodel.Chunk`
row.
"""

from __future__ import annotations

import json
from uuid import UUID

import xxhash

from aizk.chunking import Chunk as SplitterChunk
from aizk.graph.datamodel import INPUT_KIND_CONTEXTUALIZED, INPUT_KIND_RAW, Chunk as GraphChunk
from aizk.graph.fingerprints import PayloadItem, chunk_key, payload_fingerprint
from aizk.pipeline.identity import content_fingerprint

_SOURCE_ID = UUID("11111111-1111-1111-1111-111111111111")


def _item(key: str, *, input_kind: str = INPUT_KIND_RAW, text: str = "text") -> PayloadItem:
    return PayloadItem(chunk_key=key, input_kind=input_kind, text_fingerprint=content_fingerprint(text))


def test_payload_fingerprint_is_order_sensitive() -> None:
    """Reordering the same items changes the fingerprint."""
    forward = payload_fingerprint([_item("a"), _item("b")])
    reversed_ = payload_fingerprint([_item("b"), _item("a")])
    assert forward != reversed_, "payload_fingerprint must be order-sensitive, not a set fingerprint"


def test_payload_fingerprint_distinguishes_duplicate_position_count() -> None:
    """The same text at two positions differs from the same text at one position."""
    once = payload_fingerprint([_item("a", text="dup")])
    twice = payload_fingerprint([_item("a", text="dup"), _item("a", text="dup")])
    assert once != twice, "duplicate text at distinct positions must change the fingerprint"


def test_payload_fingerprint_distinguishes_input_kind() -> None:
    """An input_kind flip with identical chunk_key and text changes the fingerprint."""
    raw = payload_fingerprint([_item("a", input_kind=INPUT_KIND_RAW, text="same text")])
    contextualized = payload_fingerprint([_item("a", input_kind=INPUT_KIND_CONTEXTUALIZED, text="same text")])
    assert raw != contextualized, "input_kind participates in the fingerprint independent of the text"


def test_chunk_key_agrees_for_splitter_and_persisted_shapes() -> None:
    """A SplitterChunk and its persisted Chunk row compute the same portable content key."""
    text = "a paragraph of chunk text"
    content_hash = xxhash.xxh64(text.encode("utf-8")).hexdigest()
    heading_path = ("Section One",)
    splitter_chunk = SplitterChunk(
        content_hash=content_hash,
        source_id=str(_SOURCE_ID),
        heading_path=heading_path,
        ordinal=0,
        text=text,
        char_count=len(text),
        converted_artifact_id="output-1",
        markdown_hash_xx64="0011223344556677",
        span=(0, len(text)),
        splitter_version=1,
    )
    graph_chunk = GraphChunk(
        chunk_id=UUID("22222222-2222-2222-2222-222222222222"),
        content_hash=content_hash,
        source_id=_SOURCE_ID,
        heading_path_json=json.dumps(list(heading_path)),
        ordinal=0,
        text=text,
        char_count=len(text),
    )
    assert chunk_key(splitter_chunk) == chunk_key(graph_chunk), (
        "the same logical chunk must key identically from either shape"
    )
