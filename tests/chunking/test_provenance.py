"""Provenance and version stamping tests.

Requirement: Provenance and version stamping.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from aizk.chunking import SPLITTER_VERSION, Chunk
from tests.chunking.conftest import CONVERTED_ARTIFACT_ID, MARKDOWN_HASH_XX64

# (fixture, size_budget) pairs that exercise each distinct emission path.
_EMISSION_PATHS = [
    ("under_budget.md", 4096),  # in-budget whole body
    ("over_budget_paragraphs.md", 120),  # paragraph split
    ("oversize_paragraph.md", 120),  # sentence fallback
    ("oversize_code_block.md", 120),  # non-splittable block
    ("pre_heading_content.md", 4096),  # pre-heading content
    ("frontmatter.md", 4096),  # frontmatter
]


def test_every_chunk_has_populated_provenance(
    do_split: Callable[..., list[Chunk]],
    all_fixture_names: list[str],
) -> None:
    """Every emitted chunk across the suite has all provenance fields populated."""
    for name in all_fixture_names:
        chunks = do_split(name)
        assert chunks, f"{name} emitted no chunks"
        for chunk in chunks:
            assert chunk.converted_artifact_id
            assert chunk.markdown_hash_xx64
            assert chunk.span[1] >= chunk.span[0]
            assert chunk.splitter_version == SPLITTER_VERSION


def test_provenance_uniform_across_invocation(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """All chunks from one invocation share the input provenance and version."""
    chunks = do_split("multi_section.md")

    assert {c.converted_artifact_id for c in chunks} == {CONVERTED_ARTIFACT_ID}
    assert {c.markdown_hash_xx64 for c in chunks} == {MARKDOWN_HASH_XX64}
    assert {c.splitter_version for c in chunks} == {SPLITTER_VERSION}


def test_span_locates_chunk_in_source(
    do_split: Callable[..., list[Chunk]],
    load_fixture: Callable[[str], str],
    all_fixture_names: list[str],
) -> None:
    """Resolving a chunk's span against the source recovers the chunk's text."""
    for name in all_fixture_names:
        text = load_fixture(name)
        for chunk in do_split(name):
            assert text[chunk.span[0] : chunk.span[1]] == chunk.text


@pytest.mark.parametrize(("fixture_name", "size_budget"), _EMISSION_PATHS)
def test_provenance_per_emission_path(
    fixture_name: str,
    size_budget: int,
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Provenance is stamped on chunks emitted from every distinct path."""
    chunks = do_split(fixture_name, size_budget=size_budget)

    assert chunks
    for chunk in chunks:
        assert chunk.converted_artifact_id == CONVERTED_ARTIFACT_ID
        assert chunk.markdown_hash_xx64 == MARKDOWN_HASH_XX64
        assert chunk.splitter_version == SPLITTER_VERSION
        assert chunk.span[1] >= chunk.span[0]
