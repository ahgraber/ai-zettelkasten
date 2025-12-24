"""Hashing helpers for idempotency and markdown content."""

from __future__ import annotations

import hashlib

import xxhash


def compute_idempotency_key(
    aizk_uuid: str,
    payload_version: int,
    docling_version: str,
    config_hash: str,
) -> str:
    """Compute a stable SHA256 idempotency key.

    Args:
        aizk_uuid: Bookmark UUID.
        payload_version: Payload version for conversion.
        docling_version: Docling version string.
        config_hash: Hash of conversion configuration.

    Returns:
        Hex-encoded SHA256 digest.
    """
    raw = f"{aizk_uuid}:{payload_version}:{docling_version}:{config_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_markdown_hash(markdown_text: str) -> str:
    """Compute xxHash64 for normalized markdown content.

    Args:
        markdown_text: Raw markdown content.

    Returns:
        Hex-encoded xxHash64 digest.
    """
    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return xxhash.xxh64(normalized.encode("utf-8")).hexdigest()
