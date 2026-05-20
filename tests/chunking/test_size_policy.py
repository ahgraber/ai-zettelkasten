"""Size budget compliance tests.

Requirement: Size budget compliance with non-splittable block exception.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

import pytest

from aizk.chunking import Chunk


def test_under_budget_body_one_chunk(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A heading body within budget emits a single chunk at or below budget."""
    budget = 4096
    chunks = do_split("under_budget.md", size_budget=budget)

    assert len(chunks) == 1
    assert chunks[0].char_count <= budget


def test_splittable_over_budget_paragraph_split(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """An over-budget body of within-budget paragraphs splits without splitting a paragraph."""
    budget = 120
    chunks = do_split("over_budget_paragraphs.md", size_budget=budget)

    assert len(chunks) > 1
    assert all(c.char_count <= budget for c in chunks)

    for paragraph_prefix in (
        "Paragraph one has",
        "Paragraph two also",
        "Paragraph three rounds",
    ):
        containing = [c for c in chunks if paragraph_prefix in c.text]
        assert len(containing) == 1, f"{paragraph_prefix!r} split across chunks"


def test_oversize_non_splittable_block_kept_whole(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A fenced code block exceeding the budget is emitted as one whole chunk."""
    budget = 120
    chunks = do_split("oversize_code_block.md", size_budget=budget)

    assert len(chunks) == 1
    block = chunks[0]
    assert block.char_count > budget
    assert block.text.startswith("```text")
    assert block.text.rstrip("\n").endswith("```")
    assert "LINE 01" in block.text and "LINE 12" in block.text


def test_oversize_table_kept_whole(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A pipe table exceeding the budget is a single non-splittable chunk."""
    budget = 120
    chunks = do_split("table.md", size_budget=budget)

    assert len(chunks) == 1
    table = chunks[0]
    assert table.char_count > budget
    assert "| ---- | ---- | ---- |" in table.text
    assert "alice" in table.text and "dave" in table.text


def test_oversize_math_block_kept_whole(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A ``$$`` math block exceeding the budget is a single non-splittable chunk."""
    budget = 120
    chunks = do_split("math_block.md", size_budget=budget)

    assert len(chunks) == 1
    math = chunks[0]
    assert math.char_count > budget
    assert math.text.startswith("$$")
    assert math.text.rstrip("\n").endswith("$$")


def test_oversize_paragraph_sentence_fallback(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A single over-budget paragraph falls back to within-budget sentence chunks."""
    budget = 120
    chunks = do_split("oversize_paragraph.md", size_budget=budget)

    assert len(chunks) > 1, "sentence-fallback path not exercised"
    assert all(c.char_count <= budget for c in chunks)


def test_pathological_single_sentence_over_budget_warns(
    do_split: Callable[..., list[Chunk]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A single sentence exceeding the budget emits one over-budget chunk with a warning."""
    budget = 80
    with caplog.at_level(logging.WARNING, logger="aizk.chunking.splitter"):
        chunks = do_split("pathological_sentence.md", size_budget=budget)

    assert len(chunks) == 1
    assert chunks[0].char_count > budget

    warnings = [r for r in caplog.records if "size budget" in r.getMessage()]
    assert warnings, "expected a structured over-budget warning"
    assert warnings[0].char_count > budget
    assert warnings[0].size_budget == budget


def test_oversize_paragraph_with_inline_link_kept_whole(
    do_split: Callable[..., list[Chunk]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An over-budget paragraph with an inline link is kept whole, not split mid-link."""
    budget = 60
    with caplog.at_level(logging.WARNING, logger="aizk.chunking.splitter"):
        chunks = do_split("oversize_link_paragraph.md", size_budget=budget)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.char_count > budget
    # the link survives intact: one unbroken [text](url)
    assert chunk.text.count("[") == 1
    assert chunk.text.count("](https://example.com") == 1

    warnings = [r for r in caplog.records if "size budget" in r.getMessage()]
    assert warnings
    assert warnings[0].reason == "protected_inline"


def test_non_splittable_block_never_partial(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A code block among paragraphs appears whole in exactly one chunk and nowhere else."""
    budget = 80
    chunks = do_split("code_block_among_paragraphs.md", size_budget=budget)

    code_lines = ["CODE LINE ONE", "CODE LINE TWO", "CODE LINE THREE"]
    containing = [c for c in chunks if any(line in c.text for line in code_lines)]

    assert len(containing) == 1
    block = containing[0]
    assert all(line in block.text for line in code_lines)

    for chunk in chunks:
        if chunk is block:
            continue
        assert all(line not in chunk.text for line in code_lines)
        assert "```" not in chunk.text


def test_list_block_never_partial(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A list among paragraphs appears whole in exactly one chunk and nowhere else."""
    budget = 60
    chunks = do_split("list_among_paragraphs.md", size_budget=budget)

    items = ["alpha list item", "bravo list item", "charlie list item"]
    containing = [c for c in chunks if any(item in c.text for item in items)]

    assert len(containing) == 1
    block = containing[0]
    assert all(item in block.text for item in items)

    for chunk in chunks:
        if chunk is block:
            continue
        assert all(item not in chunk.text for item in items)


def test_oversize_table_among_paragraphs_kept_whole(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """An over-budget table among paragraphs is isolated whole; neighbors stay within budget."""
    budget = 120
    chunks = do_split("oversize_table_among_paragraphs.md", size_budget=budget)

    table_chunks = [c for c in chunks if "| ---- | ---- | ---- |" in c.text]
    assert len(table_chunks) == 1
    table = table_chunks[0]
    assert table.char_count > budget
    assert "alice" in table.text and "dave" in table.text

    neighbors = [c for c in chunks if c is not table]
    assert neighbors
    for chunk in neighbors:
        assert chunk.char_count <= budget
        assert "|" not in chunk.text
