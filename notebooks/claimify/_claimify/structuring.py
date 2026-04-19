"""Header-aware markdown structuring (MarkdownChef + DIY splitter)."""

from __future__ import annotations

import re

from .models import Section

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def split_by_headings(md: str, *, max_depth: int = 6) -> list[Section]:
    """Split markdown into Sections keyed by ATX heading hierarchy.

    Fenced code blocks are respected: `#` lines inside ``` or ~~~ fences
    do not open new sections. Lead-in text before the first heading is
    emitted with `heading_path=()`.
    """
    heading_re = re.compile(rf"^(#{{1,{max_depth}}})\s+(.+?)\s*#*\s*$")

    sections: list[Section] = []
    stack: list[str] = []
    in_fence = False

    buf_start = 0
    current_path: tuple[str, ...] = ()
    pos = 0

    for line in md.splitlines(keepends=True):
        line_len = len(line)
        stripped = line.rstrip("\r\n")

        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            pos += line_len
            continue

        if not in_fence:
            h_m = heading_re.match(stripped)
            if h_m:
                _emit(sections, md, buf_start, pos, current_path)
                level = len(h_m.group(1))
                title = h_m.group(2).strip()
                while len(stack) >= level:
                    stack.pop()
                stack.append(title)
                current_path = tuple(stack)
                buf_start = pos + line_len
                pos += line_len
                continue

        pos += line_len

    _emit(sections, md, buf_start, pos, current_path)
    return sections


def _emit(
    sections: list[Section],
    md: str,
    start: int,
    end: int,
    path: tuple[str, ...],
) -> None:
    if start >= end:
        return
    content = md[start:end]
    if not content.strip():
        return
    sections.append(
        Section(
            heading_path=path,
            content=content,
            start_index=start,
            end_index=end,
        )
    )
