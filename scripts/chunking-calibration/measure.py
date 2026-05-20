"""Measure heading-body and paragraph region sizes across a markdown corpus.

Walks every ``.md`` file under ``--corpus``, parses it with markdown-it-py
(commonmark + frontmatter plugin), partitions the document into heading-body
sections and their constituent top-level body blocks ("paragraphs" in the
splitter's sense), and records:

- ``regions.parquet``  — one row per region (heading_body or paragraph) with
  char_count and tiktoken cl100k token count.
- ``per_doc_metrics.parquet`` — one row per document with parse latency and
  region count.
- ``memory.json`` — tracemalloc snapshots taken every ``--snapshot-every``
  documents plus the final ``resource.getrusage`` peak RSS.

Outputs are written under ``--out`` (default: ``<repo>/data/chunking-calibration``,
which is gitignored).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import resource
import sys
import time
import tracemalloc
from typing import Iterable

from markdown_it import MarkdownIt
from mdit_py_plugins.front_matter import front_matter_plugin
from setproctitle import setproctitle

import pandas as pd

import tiktoken

logger = logging.getLogger("chunking-calibration.measure")

# This script lives at <repo>/scripts/chunking-calibration/measure.py; its
# outputs land in the gitignored <repo>/data/chunking-calibration directory.
_DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "chunking-calibration"


# Top-level block tokens that have a `.map` covering the entire block.
# `heading_open` is intentionally excluded — headings start a new section
# rather than contribute to the current body.
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
_BODY_BLOCK_LEAF_TYPES = frozenset(
    {
        "fence",
        "code_block",
        "hr",
        "math_block",
        "html_block",
        "front_matter",
    }
)


@dataclass
class RegionRow:
    """One row in regions.parquet."""

    doc_path: str
    doc_size_chars: int
    heading_depth: int
    region_kind: str  # "heading_body" or "paragraph"
    block_type: str  # markdown-it-py block type (e.g. "paragraph", "fence"); "section" for heading_body rows
    char_count: int
    tiktoken_cl100k_count: int


@dataclass
class PerDocRow:
    """One row in per_doc_metrics.parquet."""

    doc_path: str
    doc_size_chars: int
    parse_latency_ms: float
    region_count: int
    error: str = ""  # populated when parsing fails


@dataclass
class Section:
    """A heading body (or pre-heading region) with its top-level body blocks."""

    heading_depth: int  # 0 for pre-heading, 1-6 for h1..h6
    blocks: list[tuple[str, int, int]] = field(default_factory=list)  # (block_type, char_start, char_end)


def _compute_line_offsets(text: str) -> list[int]:
    """Return a list of character offsets for each line start in ``text``.

    Index ``i`` is the char offset of line ``i`` (0-indexed). Length is
    ``len(text.splitlines(keepends=True))``; consumers must guard against
    ``line_end`` values past the last index by clamping to ``len(text)``.
    """
    offsets: list[int] = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _slice_chars(text: str, line_offsets: list[int], line_start: int, line_end: int) -> tuple[int, int]:
    """Resolve a token's line range to (char_start, char_end) in ``text``."""
    char_start = line_offsets[line_start] if line_start < len(line_offsets) else len(text)
    char_end = line_offsets[line_end] if line_end < len(line_offsets) else len(text)
    return char_start, char_end


def parse_sections(text: str, md: MarkdownIt) -> list[Section]:
    """Partition ``text`` into heading-body sections plus their top-level blocks.

    Returns a list of sections in document order. The first section may have
    ``heading_depth == 0`` (pre-heading content); subsequent sections carry
    the heading's level (1-6) as ``heading_depth``.
    """
    tokens = md.parse(text)
    line_offsets = _compute_line_offsets(text)

    sections: list[Section] = []
    current = Section(heading_depth=0)

    for tok in tokens:
        if tok.level != 0:
            continue
        if tok.type == "heading_open":
            # Close current section before opening the new one. Always append
            # so empty sections (heading with no body) remain observable.
            sections.append(current)
            depth = int(tok.tag[1]) if tok.tag and tok.tag[0] == "h" else 0
            current = Section(heading_depth=depth)
            continue
        if tok.type in _BODY_BLOCK_OPEN_TYPES or tok.type in _BODY_BLOCK_LEAF_TYPES:
            if tok.map is None:
                continue
            line_start, line_end = tok.map
            char_start, char_end = _slice_chars(text, line_offsets, line_start, line_end)
            if char_end > char_start:
                # Strip the block type's `_open` suffix so the recorded label
                # matches the natural block name (e.g. "paragraph_open" → "paragraph").
                block_type = tok.type[: -len("_open")] if tok.type.endswith("_open") else tok.type
                current.blocks.append((block_type, char_start, char_end))

    sections.append(current)
    return sections


def _count_tokens(text: str, encoding: tiktoken.Encoding) -> int:
    """Return the cl100k token count of ``text``, disallowing special tokens."""
    return len(encoding.encode(text, disallowed_special=()))


def _emit_regions(
    text: str,
    sections: list[Section],
    doc_path: str,
    doc_size_chars: int,
    encoding: tiktoken.Encoding,
) -> list[RegionRow]:
    """Build region rows for one document's parsed sections."""
    rows: list[RegionRow] = []
    for section in sections:
        if not section.blocks:
            continue
        # Aggregate section text by concatenating block slices (preserves the
        # source content the splitter would see for the heading body).
        section_text = "".join(text[start:end] for _, start, end in section.blocks)
        section_char_count = len(section_text)
        rows.append(
            RegionRow(
                doc_path=doc_path,
                doc_size_chars=doc_size_chars,
                heading_depth=section.heading_depth,
                region_kind="heading_body",
                block_type="section",
                char_count=section_char_count,
                tiktoken_cl100k_count=_count_tokens(section_text, encoding),
            )
        )
        for block_type, start, end in section.blocks:
            block_text = text[start:end]
            rows.append(
                RegionRow(
                    doc_path=doc_path,
                    doc_size_chars=doc_size_chars,
                    heading_depth=section.heading_depth,
                    region_kind="paragraph",
                    block_type=block_type,
                    char_count=len(block_text),
                    tiktoken_cl100k_count=_count_tokens(block_text, encoding),
                )
            )
    return rows


def iter_corpus(corpus_root: Path) -> Iterable[Path]:
    """Yield every ``.md`` file under ``corpus_root`` in sorted order.

    Sorted order makes the run reproducible and aligns the memory snapshot
    cadence with a stable document ordering.
    """
    yield from sorted(p for p in corpus_root.rglob("*.md") if p.is_file())


def _snapshot_memory(label: str, doc_index: int) -> dict[str, object]:
    """Capture a tracemalloc + ru_maxrss snapshot."""
    current, peak = tracemalloc.get_traced_memory()
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "label": label,
        "doc_index": doc_index,
        "tracemalloc_current_bytes": int(current),
        "tracemalloc_peak_bytes": int(peak),
        # On macOS ru_maxrss is bytes; on Linux it's kilobytes. Record raw plus
        # the platform so downstream analysis can normalize correctly.
        "ru_maxrss_raw": int(rusage.ru_maxrss),
        "platform": sys.platform,
    }


def run(corpus_root: Path, out_dir: Path, snapshot_every: int) -> None:
    """Walk the corpus, emit region/per-doc/memory artifacts under ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md = MarkdownIt("commonmark").use(front_matter_plugin)
    encoding = tiktoken.get_encoding("cl100k_base")

    region_rows: list[RegionRow] = []
    per_doc_rows: list[PerDocRow] = []
    memory_snapshots: list[dict[str, object]] = []

    tracemalloc.start()
    memory_snapshots.append(_snapshot_memory(label="start", doc_index=0))

    docs = list(iter_corpus(corpus_root))
    logger.info("found %d markdown files under %s", len(docs), corpus_root)

    run_start = time.perf_counter()
    for idx, path in enumerate(docs, start=1):
        rel_path = str(path.relative_to(corpus_root))
        try:
            t0 = time.perf_counter()
            text = path.read_text(encoding="utf-8", errors="replace")
            sections = parse_sections(text, md)
            rows = _emit_regions(text, sections, rel_path, len(text), encoding)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            region_rows.extend(rows)
            per_doc_rows.append(
                PerDocRow(
                    doc_path=rel_path,
                    doc_size_chars=len(text),
                    parse_latency_ms=elapsed_ms,
                    region_count=len(rows),
                )
            )
        except Exception as exc:  # noqa: BLE001 — capture parser/IO failures per-doc
            logger.warning("failed to process %s: %s", rel_path, exc)
            per_doc_rows.append(
                PerDocRow(
                    doc_path=rel_path,
                    doc_size_chars=0,
                    parse_latency_ms=0.0,
                    region_count=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

        if idx % snapshot_every == 0:
            memory_snapshots.append(_snapshot_memory(label="periodic", doc_index=idx))
            logger.info("processed %d/%d docs", idx, len(docs))

    memory_snapshots.append(_snapshot_memory(label="end", doc_index=len(docs)))
    total_elapsed_s = time.perf_counter() - run_start
    tracemalloc.stop()

    regions_df = pd.DataFrame([row.__dict__ for row in region_rows])
    per_doc_df = pd.DataFrame([row.__dict__ for row in per_doc_rows])

    regions_path = out_dir / "regions.parquet"
    per_doc_path = out_dir / "per_doc_metrics.parquet"
    memory_path = out_dir / "memory.json"
    regions_df.to_parquet(regions_path, index=False)
    per_doc_df.to_parquet(per_doc_path, index=False)
    memory_path.write_text(
        json.dumps(
            {
                "snapshots": memory_snapshots,
                "total_elapsed_seconds": total_elapsed_s,
                "doc_count": len(docs),
                "snapshot_every": snapshot_every,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "wrote %d region rows, %d per-doc rows, %d memory snapshots in %.1fs",
        len(region_rows),
        len(per_doc_rows),
        len(memory_snapshots),
        total_elapsed_s,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        required=True,
        help="Path to a directory containing .md files (recursively).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help="Output directory for parquet + json artifacts (default: <repo>/data/chunking-calibration).",
    )
    parser.add_argument(
        "--snapshot-every",
        type=int,
        default=100,
        help="Take a tracemalloc snapshot every N documents (default: 100).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    setproctitle("chunking-calibration-measure")
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    corpus_root = args.corpus.resolve()
    if not corpus_root.is_dir():
        logger.error("corpus path is not a directory: %s", corpus_root)
        return 2
    run(corpus_root=corpus_root, out_dir=args.out.resolve(), snapshot_every=args.snapshot_every)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
