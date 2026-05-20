"""Shared content-hashing helpers used across pipeline stages.

The conversion stage publishes ``markdown_hash_xx64`` for converted artifacts and
the chunking stage derives ``content_hash`` for individual chunks; both reuse the
same normalization + xxh64 algorithm so identity is consistent across stages.
"""

from __future__ import annotations

import xxhash


def compute_markdown_hash(markdown_text: str) -> str:
    """Compute the xxHash64 hex digest of normalized markdown content.

    Normalization mirrors the conversion stage's hash-time policy: CRLF / CR are
    folded to LF and the text is outer-stripped before encoding. This keeps the
    digest stable against line-ending differences and trailing-whitespace edits.

    Args:
        markdown_text: Markdown content to hash.

    Returns:
        Hex-encoded xxHash64 digest (16 characters).
    """
    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return xxhash.xxh64(normalized.encode("utf-8")).hexdigest()
