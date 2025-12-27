"""Hashing helpers for idempotency and markdown content."""

from __future__ import annotations

import hashlib
import json

import xxhash

from aizk.conversion.utilities.config import ConversionConfig


def compute_idempotency_key(
    aizk_uuid: str,
    payload_version: int,
    docling_version: str,
    config: ConversionConfig,
) -> str:
    """Compute a stable SHA256 idempotency key.

    Args:
        aizk_uuid: Bookmark UUID.
        payload_version: Payload version for conversion.
        docling_version: Docling version string.
        config: Conversion configuration.

    Returns:
        Hex-encoded SHA256 digest.
    """
    config_payload = {
        key: value
        for key, value in config.model_dump().items()
        if key.startswith("docling_")
    }
    config_json = json.dumps(config_payload, sort_keys=True, separators=(",", ":"))
    raw = f"{aizk_uuid}:{payload_version}:{docling_version}:{config_json}"
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


def compute_config_hash(config_payload: dict[str, object]) -> str:
    """Compute a deterministic hash for conversion configuration payload.

    Args:
        config_payload: Configuration values that influence conversion output.

    Returns:
        Hex-encoded SHA256 digest truncated to 16 characters.
    """
    serialized = json.dumps(config_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
