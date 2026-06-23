"""Document-structure splitter for converted Markdown artifacts.

:func:`split` is a deterministic, pure function: it parses an already-normalized
Markdown artifact with ``markdown-it-py`` (CommonMark + frontmatter), walks the
top-level token stream into heading-body sections, and emits ordered
:class:`~aizk.chunking.datamodel.Chunk` objects under a per-chunk character
budget. It performs no I/O and depends on no per-process state.

Emission paths:

- a heading body within budget becomes one chunk;
- an over-budget body is split on top-level block boundaries (one chunk per block);
- a splittable paragraph that itself exceeds the budget falls back to
  sentence-level splitting via :class:`chonkie.SentenceChunker`;
- an over-budget paragraph that carries an inline link, image, code span, or math
  span is kept whole instead — sentence splitting could otherwise cut the construct
  apart, and markdown-it exposes no inline source offsets to split around it;
- a non-splittable block (code, table, list, blockquote, math, raw HTML) is kept
  whole even when it exceeds the budget;
- frontmatter and pre-heading content are emitted under the empty heading path.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import logging

from chonkie import SentenceChunker
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.front_matter import front_matter_plugin

from aizk.chunking._version import DEFAULT_SIZE_BUDGET, SPLITTER_VERSION
from aizk.chunking.datamodel import Chunk
from aizk.utilities.hashing import compute_markdown_hash

logger = logging.getLogger(__name__)

# A single parser instance is reused across invocations; ``parse`` returns a
# fresh token list per call, so reuse does not introduce shared state.
# CommonMark plus: GFM tables and ``$$`` math (so both tokenize as their own
# non-splittable blocks rather than as splittable paragraphs) and YAML
# frontmatter. TOML (``+++``) frontmatter is detected separately; see
# ``_detect_toml_frontmatter``.
_MARKDOWN = MarkdownIt("commonmark").enable("table").use(front_matter_plugin).use(dollarmath_plugin)

# Top-level container blocks whose opening token's ``.map`` covers the whole block.
_BODY_BLOCK_OPEN_TYPES = frozenset(
    {
        "paragraph_open",
        "bullet_list_open",
        "ordered_list_open",
        "blockquote_open",
        "table_open",
        "dl_open",
    }
)
# Self-contained top-level blocks (no matching close token).
_BODY_BLOCK_LEAF_TYPES = frozenset(
    {
        "fence",
        "code_block",
        "hr",
        "math_block",
        "html_block",
    }
)
# Only paragraphs may be split below the block level (paragraph -> sentences).
_SPLITTABLE_BLOCK_KINDS = frozenset({"paragraph"})
# Inline construct tokens whose source offsets markdown-it does not expose; a
# sentence split landing inside one would break it (a dangling ``](url)`` or
# half a code span). A paragraph containing any of these is kept whole instead.
_PROTECTED_INLINE_TYPES = frozenset({"link_open", "image", "code_inline", "math_inline"})


@dataclass(frozen=True)
class _Block:
    """A top-level body block resolved to character offsets in the source."""

    kind: str  # normalized block name, e.g. "paragraph", "fence", "blockquote"
    char_start: int
    char_end: int
    # True for a paragraph carrying inline links/images/code/math that the
    # sentence splitter cannot divide safely; such a paragraph is non-splittable.
    protected: bool = False


@dataclass
class _Section:
    """A heading body: its heading path and its top-level body blocks."""

    heading_path: tuple[str, ...]
    blocks: list[_Block] = field(default_factory=list)


def _compute_line_offsets(text: str) -> list[int]:
    """Return character offsets for each line start, plus a trailing sentinel.

    ``offsets[i]`` is the character offset where line ``i`` begins; the final
    entry equals ``len(text)`` so an exclusive ``token.map`` line end always
    resolves without bounds-checking.
    """
    offsets: list[int] = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _slice_chars(text: str, line_offsets: list[int], line_start: int, line_end: int) -> tuple[int, int]:
    """Resolve a token's ``[line_start, line_end)`` range to character offsets."""
    char_start = line_offsets[line_start] if line_start < len(line_offsets) else len(text)
    char_end = line_offsets[line_end] if line_end < len(line_offsets) else len(text)
    return char_start, char_end


def _detect_toml_frontmatter(text: str) -> int:
    """Return the end offset of a leading TOML (``+++``) frontmatter block, or 0.

    The frontmatter plugin only recognizes YAML (``---``) fences, so TOML fences
    are detected here; this keeps heading-shaped lines inside a ``+++`` block from
    being parsed as section boundaries. The block must open on the first line and
    have a matching closing ``+++`` line; otherwise no frontmatter is reported.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "+++":
        return 0
    offset = len(lines[0])
    for line in lines[1:]:
        offset += len(line)
        if line.rstrip("\r\n") == "+++":
            return offset
    return 0


def _paragraph_has_protected_inline(inline_token: object) -> bool:
    """Return True if a paragraph's inline token carries a non-divisible construct.

    markdown-it exposes no source offsets for inline children, so a sentence split
    could cut through a link, image, code span, or inline math. Detecting their
    presence by token type lets the caller keep such a paragraph whole.
    """
    children = getattr(inline_token, "children", None) or []
    return any(child.type in _PROTECTED_INLINE_TYPES for child in children)


def _parse_structure(text: str) -> tuple[list[_Block], list[_Section]]:
    """Walk the token stream into frontmatter blocks and heading-body sections.

    Headings nested inside non-section contexts (blockquotes, list items) carry a
    non-zero token ``level`` and are skipped here, so only outer Markdown sections
    delimit ``heading_path``. Heading syntax inside fenced code never tokenizes as
    a heading at all. A leading TOML frontmatter block is split off before parsing
    and all resulting offsets are shifted back into the original text.
    """
    frontmatter_blocks: list[_Block] = []
    base = _detect_toml_frontmatter(text)
    if base:
        frontmatter_blocks.append(_Block("front_matter", 0, base))
    body = text[base:]

    tokens = _MARKDOWN.parse(body)
    line_offsets = _compute_line_offsets(body)

    sections: list[_Section] = []
    heading_stack: list[tuple[int, str]] = []
    current = _Section(heading_path=())

    index = 0
    token_count = len(tokens)
    while index < token_count:
        tok = tokens[index]
        index += 1
        if tok.level != 0:
            continue

        if tok.type == "heading_open":
            sections.append(current)
            level = int(tok.tag[1]) if tok.tag and tok.tag[0] == "h" else 0
            heading_text = tokens[index].content if index < token_count else ""
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, heading_text))
            current = _Section(heading_path=tuple(htext for _, htext in heading_stack))
            continue

        if tok.map is None:
            continue

        char_start, char_end = _slice_chars(body, line_offsets, tok.map[0], tok.map[1])
        if char_end <= char_start:
            continue
        char_start += base
        char_end += base

        if tok.type == "front_matter":
            frontmatter_blocks.append(_Block("front_matter", char_start, char_end))
            continue

        if tok.type in _BODY_BLOCK_OPEN_TYPES or tok.type in _BODY_BLOCK_LEAF_TYPES:
            kind = tok.type[: -len("_open")] if tok.type.endswith("_open") else tok.type
            protected = (
                tok.type == "paragraph_open" and index < token_count and _paragraph_has_protected_inline(tokens[index])
            )
            current.blocks.append(_Block(kind, char_start, char_end, protected=protected))

    sections.append(current)
    return frontmatter_blocks, sections


def split(
    markdown_text: str,
    *,
    source_id: str,
    converted_artifact_id: str,
    markdown_hash_xx64: str,
    size_budget: int = DEFAULT_SIZE_BUDGET,
) -> list[Chunk]:
    """Split a converted Markdown artifact into ordered structural chunks.

    The artifact is assumed to be already whitespace-normalized by the conversion
    stage; the splitter does not re-normalize the body. Output is deterministic:
    identical inputs always produce identical chunks in identical order.

    Args:
        markdown_text: The converted, normalized Markdown artifact.
        source_id: Logical document identifier stamped on every chunk.
        converted_artifact_id: Source-artifact identifier stamped on every chunk.
        markdown_hash_xx64: Source-artifact content hash stamped on every chunk.
        size_budget: Maximum per-chunk character count, except for a non-splittable
            block that individually exceeds it and for a paragraph that can only be
            split by cutting through an inline link, image, code span, or math span.

    Returns:
        Chunks in document order. Within a shared ``heading_path`` they are
        ordered by ``ordinal``.
    """
    frontmatter_blocks, sections = _parse_structure(markdown_text)
    ordinal_counters: dict[tuple[str, ...], int] = defaultdict(int)
    chunks: list[Chunk] = []

    def warn_over_budget(heading_path: tuple[str, ...], char_count: int, reason: str) -> None:
        logger.warning(
            "chunk exceeds size budget",
            extra={
                "source_id": source_id,
                "converted_artifact_id": converted_artifact_id,
                "heading_path": heading_path,
                "char_count": char_count,
                "size_budget": size_budget,
                "reason": reason,
            },
        )

    def emit(heading_path: tuple[str, ...], chunk_text: str, span: tuple[int, int]) -> None:
        ordinal = ordinal_counters[heading_path]
        ordinal_counters[heading_path] += 1
        content_hash = compute_markdown_hash(chunk_text)
        chunks.append(
            Chunk(
                content_hash=content_hash,
                source_id=source_id,
                heading_path=heading_path,
                ordinal=ordinal,
                text=chunk_text,
                char_count=len(chunk_text),
                converted_artifact_id=converted_artifact_id,
                markdown_hash_xx64=markdown_hash_xx64,
                span=span,
                splitter_version=SPLITTER_VERSION,
            )
        )

    def emit_sentence_fallback(heading_path: tuple[str, ...], block: _Block) -> None:
        block_text = markdown_text[block.char_start : block.char_end]
        chunker = SentenceChunker(tokenizer="character", chunk_size=size_budget, chunk_overlap=0)
        clusters = chunker(block_text)
        if not clusters:
            emit(heading_path, block_text, (block.char_start, block.char_end))
            return
        for cluster in clusters:
            seg_start = block.char_start + cluster.start_index
            seg_end = block.char_start + cluster.end_index
            seg_text = markdown_text[seg_start:seg_end]
            if len(seg_text) > size_budget:
                warn_over_budget(heading_path, len(seg_text), reason="sentence_fallback")
            emit(heading_path, seg_text, (seg_start, seg_end))

    for block in frontmatter_blocks:
        emit((), markdown_text[block.char_start : block.char_end], (block.char_start, block.char_end))

    for section in sections:
        if not section.blocks:
            continue
        path = section.heading_path
        body_start = section.blocks[0].char_start
        body_end = section.blocks[-1].char_end
        body_text = markdown_text[body_start:body_end]

        if len(body_text) <= size_budget:
            emit(path, body_text, (body_start, body_end))
            continue

        for block in section.blocks:
            block_text = markdown_text[block.char_start : block.char_end]
            span = (block.char_start, block.char_end)
            if block.kind not in _SPLITTABLE_BLOCK_KINDS or len(block_text) <= size_budget:
                emit(path, block_text, span)
            elif block.protected:
                # Splitting would cut an inline construct; keep the paragraph whole.
                warn_over_budget(path, len(block_text), reason="protected_inline")
                emit(path, block_text, span)
            else:
                emit_sentence_fallback(path, block)

    return chunks
