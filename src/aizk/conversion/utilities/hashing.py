"""Hashing helpers for idempotency and markdown content."""

from __future__ import annotations

import hashlib
import json

import xxhash

from aizk.conversion.utilities.config import DoclingConverterConfig


def _docling_config_payload(config: DoclingConverterConfig) -> dict[str, object]:
    """Return the subset of DoclingConverterConfig fields that affect Docling output.

    Excludes endpoint URL and API key: these identify the picture-description provider
    but do not affect replayable output, and the API key is a secret that must not be
    persisted into the manifest.
    """
    return {
        "pdf_max_pages": config.pdf_max_pages,
        "ocr_enabled": config.ocr_enabled,
        "table_structure_enabled": config.table_structure_enabled,
        "picture_description_model": config.picture_description_model,
        "picture_timeout": config.picture_timeout,
        "picture_classification_enabled": config.picture_classification_enabled,
    }


def build_output_config_snapshot(
    config: DoclingConverterConfig,
    *,
    picture_description_enabled: bool,
) -> dict[str, object]:
    """Build the canonical replayable config payload for hashing and manifests."""
    return {
        **_docling_config_payload(config),
        "picture_description_enabled": picture_description_enabled,
    }


def compute_idempotency_key(
    source_ref_hash: str,
    converter_name: str,
    config_snapshot: dict[str, object],
) -> str:
    """Compute a stable SHA256 idempotency key.

    Args:
        source_ref_hash: SHA-256 hash of the source ref's dedup payload.
        converter_name: Name of the converter (e.g. "docling").
        config_snapshot: Converter-supplied output-affecting config dict.

    Returns:
        Hex-encoded SHA256 digest.
    """
    config_json = json.dumps(config_snapshot, sort_keys=True, separators=(",", ":"))
    raw = f"{source_ref_hash}:{converter_name}:{config_json}"
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
