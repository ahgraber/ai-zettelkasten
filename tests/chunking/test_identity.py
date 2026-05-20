"""Chunk identity derivation tests.

Requirement: Chunk identity is derived from address and content.
"""

from __future__ import annotations

from aizk.chunking.datamodel import derive_chunk_id


def test_same_address_same_content_yields_same_chunk_id() -> None:
    """Identical address and content hash derive an identical chunk_id."""
    first = derive_chunk_id("doc", ("Intro", "Details"), 2, "deadbeefcafef00d")
    second = derive_chunk_id("doc", ("Intro", "Details"), 2, "deadbeefcafef00d")

    assert first == second


def test_same_address_different_content_yields_different_chunk_id() -> None:
    """Varying only the content hash changes the chunk_id."""
    base = derive_chunk_id("doc", ("Intro",), 0, "1111111111111111")
    changed = derive_chunk_id("doc", ("Intro",), 0, "2222222222222222")

    assert base != changed


def test_different_address_same_content_yields_different_chunk_id() -> None:
    """Varying any single address axis changes the chunk_id at equal content."""
    content = "0123456789abcdef"
    base = derive_chunk_id("doc", ("Intro",), 0, content)

    assert derive_chunk_id("doc", ("Renamed",), 0, content) != base  # heading_path axis
    assert derive_chunk_id("doc", ("Intro",), 1, content) != base  # ordinal axis
    assert derive_chunk_id("other-doc", ("Intro",), 0, content) != base  # doc_id axis
