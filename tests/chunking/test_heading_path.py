"""Heading-path edge case tests.

Requirement: Defined behavior for heading-path edge cases.
"""

from __future__ import annotations

from collections.abc import Callable

from aizk.chunking import Chunk


def test_pre_heading_content_empty_path(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Pre-heading content is a chunk with empty heading path, ordered first."""
    chunks = do_split("pre_heading_content.md")

    assert chunks[0].heading_path == ()
    assert "Intro paragraph" in chunks[0].text
    titled = [c for c in chunks if c.heading_path != ()]
    assert titled
    assert all(chunks[0].span[0] < c.span[0] for c in titled)


def test_skipped_levels_actual_nesting(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A level-1 then level-3 heading nests as ("A", "C") with no inferred level."""
    chunks = do_split("skipped_levels.md")

    body_c = next(c for c in chunks if "Body under C." in c.text)
    assert body_c.heading_path == ("A", "C")


def test_heading_in_fenced_code_not_boundary(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Heading syntax inside a fenced code block does not start a section."""
    chunks = do_split("heading_in_fenced_code.md")

    assert all(c.heading_path == ("Outer",) for c in chunks)
    assert any("def f():" in c.text for c in chunks)


def test_heading_in_blockquote_not_boundary(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Heading syntax inside a blockquote does not start a section."""
    chunks = do_split("heading_in_blockquote.md")

    assert all(c.heading_path == ("Outer",) for c in chunks)
    assert any("quoted heading-shaped text" in c.text for c in chunks)


def test_heading_in_list_not_boundary(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Heading syntax inside a list item does not start a section."""
    chunks = do_split("heading_in_list.md")

    assert all(c.heading_path == ("Outer",) for c in chunks)
    assert any("list-item heading-shaped text" in c.text for c in chunks)


def test_empty_heading_body_no_chunk(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A heading with no body emits no chunk but remains in descendant paths."""
    chunks = do_split("empty_heading_body.md")

    assert all(c.heading_path != ("A",) for c in chunks)
    body = next(c for c in chunks if "Body under B only." in c.text)
    assert body.heading_path == ("A", "B")


def test_heading_less_document_empty_path(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Every chunk in a heading-less document carries the empty heading path."""
    chunks = do_split("heading_less.md")

    assert chunks
    assert all(c.heading_path == () for c in chunks)


def test_frontmatter_emitted_as_chunk(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Frontmatter is a chunk with empty path and ordinal 0, ordered before all others."""
    chunks = do_split("frontmatter.md")

    frontmatter = chunks[0]
    assert frontmatter.heading_path == ()
    assert frontmatter.ordinal == 0
    assert frontmatter.text.startswith("---")
    assert "title: Sample" in frontmatter.text


def test_toml_frontmatter_emitted_as_chunk(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """TOML (+++) frontmatter is a chunk with empty path and ordinal 0, ordered first."""
    chunks = do_split("frontmatter_toml.md")

    frontmatter = chunks[0]
    assert frontmatter.heading_path == ()
    assert frontmatter.ordinal == 0
    assert frontmatter.text.startswith("+++")
    assert "title = " in frontmatter.text
    # the heading-shaped comment line inside the TOML block is not a section boundary
    assert all(c.heading_path != ("# a toml comment, not a heading",) for c in chunks)
    assert all("toml comment" not in str(c.heading_path) for c in chunks)
