"""Unit tests for hashing utilities."""

import hashlib
import json

from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import (
    build_output_config_snapshot,
    compute_idempotency_key,
    compute_markdown_hash,
)


def test_compute_idempotency_key_matches_sha256():
    """Verify formula: sha256(source_ref_hash:converter_name:config_json)."""
    snapshot = {"a": 1, "b": True}
    source_hash = "abc123"
    converter = "docling"
    config_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(f"{source_hash}:{converter}:{config_json}".encode()).hexdigest()
    assert compute_idempotency_key(source_hash, converter, snapshot) == expected


def test_compute_idempotency_key_differs_by_source_ref_hash():
    snapshot = {"x": 1}
    assert compute_idempotency_key("hash_a", "docling", snapshot) != compute_idempotency_key(
        "hash_b", "docling", snapshot
    )


def test_compute_idempotency_key_differs_by_converter_name():
    snapshot = {"x": 1}
    assert compute_idempotency_key("hash", "docling", snapshot) != compute_idempotency_key("hash", "marker", snapshot)


def test_compute_idempotency_key_stable_for_identical_inputs():
    snapshot = {"x": 1}
    key_a = compute_idempotency_key("hash", "docling", snapshot)
    key_b = compute_idempotency_key("hash", "docling", snapshot)
    assert key_a == key_b


def test_compute_markdown_hash_normalizes_line_endings_and_trim():
    text_a = "Line 1\r\nLine 2\r\n"
    text_b = "Line 1\nLine 2"
    assert compute_markdown_hash(text_a) == compute_markdown_hash(text_b)


def test_build_output_config_snapshot_matches_manifest_contract():
    config = ConversionConfig(_env_file=None)
    snapshot = build_output_config_snapshot(config, picture_description_enabled=True)
    assert set(snapshot) == {
        "docling_pdf_max_pages",
        "docling_enable_ocr",
        "docling_enable_table_structure",
        "docling_picture_description_model",
        "docling_picture_timeout",
        "docling_enable_picture_classification",
        "picture_description_enabled",
    }
    assert snapshot["picture_description_enabled"] is True
    assert snapshot["docling_enable_picture_classification"] is True


def test_build_output_config_snapshot_omits_provider_identity_and_credentials():
    """Provider identity and credentials MUST NOT appear in the manifest snapshot, even when configured."""
    config = ConversionConfig(
        _env_file=None,
        DOCLING_PICTURE_DESCRIPTION_BASE_URL="https://provider.example.com/v1",
        DOCLING_PICTURE_DESCRIPTION_API_KEY="sk-real-looking-value",
    )
    snapshot = build_output_config_snapshot(config, picture_description_enabled=True)
    assert "docling_picture_description_base_url" not in snapshot
    assert "docling_picture_description_api_key" not in snapshot
