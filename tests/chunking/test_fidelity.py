"""Structural fidelity tests.

Requirement: Structural fidelity to the source artifact.
"""

from __future__ import annotations

from collections.abc import Callable

from aizk.chunking import Chunk

# Distinct body sentinels in multi_section.md, grouped by the heading path they
# belong to. Each body region appears verbatim in the fixture exactly once.
_SECTION_SENTINELS: dict[tuple[str, ...], list[str]] = {
    ("Alpha",): ["Alpha first paragraph sentinel.", "Alpha second paragraph sentinel."],
    ("Beta",): ["Beta first paragraph sentinel."],
    ("Beta", "Beta Child"): ["Beta child paragraph sentinel."],
    ("Gamma",): ["Gamma only paragraph sentinel."],
}


def test_body_regions_partitioned_across_chunks(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Every source body region appears in exactly one chunk, and spans do not overlap."""
    chunks = do_split("multi_section.md")

    for sentinels in _SECTION_SENTINELS.values():
        for sentinel in sentinels:
            containing = [c for c in chunks if sentinel in c.text]
            assert len(containing) == 1, f"{sentinel!r} found in {len(containing)} chunks"

    spans = sorted(c.span for c in chunks)
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        assert prev_end <= next_start, "chunk spans overlap"


def test_no_chunk_spans_heading_boundary(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """No chunk contains body text from more than one heading's body."""
    chunks = do_split("multi_section.md")

    for chunk in chunks:
        own = _SECTION_SENTINELS[chunk.heading_path]
        others = [
            sentinel
            for path, sentinels in _SECTION_SENTINELS.items()
            if path != chunk.heading_path
            for sentinel in sentinels
        ]
        assert any(sentinel in chunk.text for sentinel in own)
        assert all(sentinel not in chunk.text for sentinel in others)


def test_thematic_break_not_section_boundary(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """A `---` thematic break is a discrete non-splittable block, not a section boundary."""
    chunks = do_split("thematic_break.md", size_budget=50)

    assert all(c.heading_path == ("Section",) for c in chunks)
    rule_chunks = [c for c in chunks if c.text.strip() == "---"]
    assert len(rule_chunks) == 1
    assert any("before the thematic break" in c.text for c in chunks)
    assert any("after the thematic break" in c.text for c in chunks)


def test_chunk_order_reproduces_source_order(
    do_split: Callable[..., list[Chunk]],
) -> None:
    """Sorting by (heading path in document order, ordinal) yields monotonic spans."""
    chunks = do_split("multi_section.md")

    document_order: dict[tuple[str, ...], int] = {}
    for chunk in chunks:
        document_order.setdefault(chunk.heading_path, len(document_order))

    ordered = sorted(chunks, key=lambda c: (document_order[c.heading_path], c.ordinal))
    starts = [c.span[0] for c in ordered]

    assert starts == sorted(starts)
