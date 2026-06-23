"""Document-structure chunking for converted Markdown artifacts.

This package implements the foundational splitter described by the chunking spec
(``.specs/specs/chunking/spec.md``). It turns an already-normalized Markdown
artifact into ordered, deterministically-produced structural chunks suitable
for downstream embedding and retrieval. Chunk identity is a stable surrogate
assigned at persistence, not a splitter output.

Public surface:

- :func:`split` — the deterministic, pure splitter entry point.
- :class:`Chunk` — the immutable chunk data model.
- :data:`SPLITTER_VERSION` — the splitter's behavior version.
- :data:`DEFAULT_SIZE_BUDGET` — the calibrated default per-chunk character budget.
"""

from __future__ import annotations

from aizk.chunking._version import DEFAULT_SIZE_BUDGET, SPLITTER_VERSION
from aizk.chunking.datamodel import Chunk
from aizk.chunking.splitter import split

__all__ = [
    "DEFAULT_SIZE_BUDGET",
    "SPLITTER_VERSION",
    "Chunk",
    "split",
]
