"""Tests for the header-aware markdown splitter."""

from __future__ import annotations

from textwrap import dedent

from _claimify.structuring import split_by_headings


def test_lead_in_and_nested_headings():
    md = dedent(
        """\
        lead in paragraph
        second lead-in line

        # Top
        top body

        ## Alpha
        alpha body

        ### Alpha One
        a1 body

        ## Beta
        beta body
        """
    )

    sections = split_by_headings(md)
    paths = [s.heading_path for s in sections]

    assert paths == [
        (),
        ("Top",),
        ("Top", "Alpha"),
        ("Top", "Alpha", "Alpha One"),
        ("Top", "Beta"),
    ]
    assert sections[0].content.startswith("lead in paragraph")
    assert sections[-1].content.strip() == "beta body"


def test_heading_inside_fenced_code_is_ignored():
    md = dedent(
        """\
        # Real
        before fence

        ```python
        # not a heading
        x = 1
        ```

        after fence

        ## After
        tail
        """
    )

    sections = split_by_headings(md)
    paths = [s.heading_path for s in sections]

    assert paths == [("Real",), ("Real", "After")]
    assert "# not a heading" in sections[0].content
    assert "x = 1" in sections[0].content


def test_skip_level_preserves_ancestor_path():
    md = dedent(
        """\
        # Top
        t

        ### Deep
        d
        """
    )
    sections = split_by_headings(md)
    assert [s.heading_path for s in sections] == [("Top",), ("Top", "Deep")]


def test_empty_sections_are_dropped():
    md = dedent(
        """\
        # A

        ## B
        b body
        """
    )
    sections = split_by_headings(md)
    assert [s.heading_path for s in sections] == [("A", "B")]


def test_tilde_fence_is_respected():
    md = dedent(
        """\
        # Root
        intro

        ~~~
        # fake
        ~~~

        ## Child
        c
        """
    )
    sections = split_by_headings(md)
    paths = [s.heading_path for s in sections]
    assert paths == [("Root",), ("Root", "Child")]
    assert "# fake" in sections[0].content
