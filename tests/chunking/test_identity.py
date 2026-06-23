"""Chunk content-key (sameness-key fingerprint) derivation tests.

Requirement: Chunk identity is a stable surrogate reused by a sameness-key. The
splitter no longer derives identity; ``derive_chunk_content_key`` is the portable
sameness-key fingerprint persistence reuses surrogate identities by and that
downstream derivation keys embed in place of the database-local surrogate. These
guard that the fingerprint is a deterministic function of the sameness tuple
``(source_id, heading_path, ordinal, content_hash)`` alone — the portability proxy
for surrogate reuse on any backend.
"""

from __future__ import annotations

from aizk.chunking.datamodel import derive_chunk_content_key


def test_same_address_same_content_yields_same_content_key() -> None:
    """Identical address and content hash derive an identical content key."""
    first = derive_chunk_content_key("doc", ("Intro", "Details"), 2, "deadbeefcafef00d")
    second = derive_chunk_content_key("doc", ("Intro", "Details"), 2, "deadbeefcafef00d")

    assert first == second


def test_same_address_different_content_yields_different_content_key() -> None:
    """Varying only the content hash changes the content key."""
    base = derive_chunk_content_key("doc", ("Intro",), 0, "1111111111111111")
    changed = derive_chunk_content_key("doc", ("Intro",), 0, "2222222222222222")

    assert base != changed


def test_different_address_same_content_yields_different_content_key() -> None:
    """Varying any single address axis changes the content key at equal content."""
    content = "0123456789abcdef"
    base = derive_chunk_content_key("doc", ("Intro",), 0, content)

    assert derive_chunk_content_key("doc", ("Renamed",), 0, content) != base  # heading_path axis
    assert derive_chunk_content_key("doc", ("Intro",), 1, content) != base  # ordinal axis
    assert derive_chunk_content_key("other-doc", ("Intro",), 0, content) != base  # source_id axis


def test_chunk_id_no_db_local_input() -> None:
    """The reuse key depends only on the sameness tuple — the portability proxy for chunk_id.

    Surrogate ``chunk_id`` reuse is decided by this content key, so it must exclude
    every database-local or generation-varying input (surrogate ids, run ids, the
    consumed artifact locator, the chunk's ``span``, the ``splitter_version``). The
    same logical chunk content therefore computes the same reuse key on any
    backend, regardless of the surrogate values a particular database assigned.
    """
    sameness = ("11111111-1111-1111-1111-111111111111", ("Section",), 3, "0011223344556677")

    key = derive_chunk_content_key(*sameness)

    # Recomputed identically with no other input available — the function admits no
    # surrogate, run id, span, locator, or version axis to begin with.
    assert derive_chunk_content_key(*sameness) == key
