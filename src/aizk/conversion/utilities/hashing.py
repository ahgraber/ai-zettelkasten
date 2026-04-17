"""Hashing helpers for idempotency and markdown content."""

from __future__ import annotations

import hashlib
import json

import xxhash

from aizk.conversion.utilities.config import ConversionConfig

# TODO(PR-9): _docling_config_payload and build_output_config_snapshot are
# Docling-specific helpers.  When the env-var namespace is renamed
# (AIZK_DOCLING_* → AIZK_CONVERTER__DOCLING__*) and the converter interface
# gains a config_snapshot() method, these should move onto DoclingConverter.
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
    source_ref_hash: str,
    converter_name: str,
    config_snapshot: dict[str, object],
) -> str:
    """Compute a stable SHA-256 idempotency key for a conversion job.

    The key incorporates the three things that determine whether two submissions
    are semantically identical: the canonical fetch identity (``source_ref_hash``),
    the converter in use, and the converter's output-affecting config snapshot.

    Args:
        source_ref_hash: SHA-256 of the submitted SourceRef's canonical dedup
            payload.  See ``aizk.conversion.core.source_ref.compute_source_ref_hash``.
        converter_name: Name the converter was registered under
            (e.g. ``"docling"``).  Included so different converters on the same
            source produce distinct keys.
        config_snapshot: Converter-supplied dict of output-affecting config
            fields.  For Docling this is the result of
            ``build_output_config_snapshot(config, picture_description_enabled=...)``
            or equivalently ``DoclingConverter.config_snapshot()``.

    Returns:
        Hex-encoded SHA-256 digest.
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
