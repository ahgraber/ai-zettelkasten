"""Hashing helpers for idempotency and markdown content."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

import xxhash

from aizk.conversion.utilities.config import ConversionConfig

_OUTPUT_IRRELEVANT_DOCLING_FIELDS = frozenset(
    {
        "docling_picture_description_base_url",
        "docling_picture_description_api_key",
    }
)


def _docling_config_payload(config: ConversionConfig) -> dict[str, object]:
    """Return the subset of config fields that affect Docling output.

    Excludes endpoint URL and API key: these identify the picture-description provider
    but do not affect replayable output, and the API key is a secret that must not be
    persisted into the manifest.
    """
    return {
        key: value
        for key, value in config.model_dump().items()
        if key.startswith("docling_") and key not in _OUTPUT_IRRELEVANT_DOCLING_FIELDS
    }


def build_output_config_snapshot(
    config: ConversionConfig,
    *,
    picture_description_enabled: bool,
) -> dict[str, object]:
    """Build the canonical replayable config payload for hashing and manifests."""
    return {
        **_docling_config_payload(config),
        "picture_description_enabled": picture_description_enabled,
    }


def compute_idempotency_key(
    aizk_uuid: UUID,
    payload_version: int,
    config: ConversionConfig,
    *,
    picture_description_enabled: bool,
) -> str:
    """Compute a stable SHA256 idempotency key.

    Args:
        aizk_uuid: Source UUID.
        payload_version: Payload version for conversion.
        config: Conversion configuration.
        picture_description_enabled: Whether picture description via chat completions is active.
            Affects Markdown output (figure alt-text), so must be part of the key.

    Returns:
        Hex-encoded SHA256 digest.
    """
    from importlib.metadata import version

    docling_version = version("docling")

    config_snapshot = build_output_config_snapshot(
        config,
        picture_description_enabled=picture_description_enabled,
    )
    config_json = json.dumps(config_snapshot, sort_keys=True, separators=(",", ":"))

    raw = f"{str(aizk_uuid)}:{payload_version}:{docling_version}:{config_json}"

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
