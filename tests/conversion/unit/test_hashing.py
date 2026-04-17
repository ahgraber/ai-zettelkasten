"""Unit tests for hashing utilities."""

import hashlib
import json

import pytest

from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import (
    build_output_config_snapshot,
    compute_idempotency_key,
    compute_markdown_hash,
)


def _expected_key(source_ref_hash: str, converter_name: str, config_snapshot: dict) -> str:
    config_json = json.dumps(config_snapshot, sort_keys=True, separators=(",", ":"))
    raw = f"{source_ref_hash}:{converter_name}:{config_json}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _docling_snapshot(config: ConversionConfig, *, picture_description_enabled: bool) -> dict:
    return build_output_config_snapshot(
        config,
        picture_description_enabled=picture_description_enabled,
    )


def test_compute_idempotency_key_matches_sha256_picture_description_disabled():
    config = ConversionConfig(_env_file=None)
    snapshot = _docling_snapshot(config, picture_description_enabled=False)
    key = compute_idempotency_key(
        source_ref_hash="ref-hash-1",
        converter_name="docling",
        config_snapshot=snapshot,
    )
    assert key == _expected_key("ref-hash-1", "docling", snapshot)


def test_compute_idempotency_key_matches_sha256_picture_description_enabled():
    config = ConversionConfig(_env_file=None)
    snapshot = _docling_snapshot(config, picture_description_enabled=True)
    key = compute_idempotency_key(
        source_ref_hash="ref-hash-1",
        converter_name="docling",
        config_snapshot=snapshot,
    )
    assert key == _expected_key("ref-hash-1", "docling", snapshot)


def test_compute_idempotency_key_differs_by_picture_description_flag():
    config = ConversionConfig(_env_file=None)
    key_off = compute_idempotency_key(
        source_ref_hash="ref-hash-1",
        converter_name="docling",
        config_snapshot=_docling_snapshot(config, picture_description_enabled=False),
    )
    key_on = compute_idempotency_key(
        source_ref_hash="ref-hash-1",
        converter_name="docling",
        config_snapshot=_docling_snapshot(config, picture_description_enabled=True),
    )
    assert key_off != key_on


def test_compute_idempotency_key_stable_for_identical_inputs():
    config = ConversionConfig(_env_file=None)
    snapshot = _docling_snapshot(config, picture_description_enabled=True)
    key_a = compute_idempotency_key(
        source_ref_hash="ref-hash-abc",
        converter_name="docling",
        config_snapshot=snapshot,
    )
    key_b = compute_idempotency_key(
        source_ref_hash="ref-hash-abc",
        converter_name="docling",
        config_snapshot=snapshot,
    )
    assert key_a == key_b


@pytest.mark.parametrize("enabled", [True, False])
def test_compute_idempotency_key_differs_by_source_ref_hash(enabled: bool):
    config = ConversionConfig(_env_file=None)
    snapshot = _docling_snapshot(config, picture_description_enabled=enabled)
    key_a = compute_idempotency_key(
        source_ref_hash="ref-hash-1",
        converter_name="docling",
        config_snapshot=snapshot,
    )
    key_b = compute_idempotency_key(
        source_ref_hash="ref-hash-2",
        converter_name="docling",
        config_snapshot=snapshot,
    )
    assert key_a != key_b


def test_compute_idempotency_key_differs_by_converter_name():
    config = ConversionConfig(_env_file=None)
    snapshot = _docling_snapshot(config, picture_description_enabled=True)
    key_docling = compute_idempotency_key(
        source_ref_hash="ref-hash-1",
        converter_name="docling",
        config_snapshot=snapshot,
    )
    key_other = compute_idempotency_key(
        source_ref_hash="ref-hash-1",
        converter_name="marker",
        config_snapshot=snapshot,
    )
    assert key_docling != key_other


def test_compute_idempotency_key_stable_when_only_base_url_rotates():
    """Endpoint identity does not affect replayable output — key must not change when it rotates."""
    config_a = ConversionConfig(
        _env_file=None,
        DOCLING_PICTURE_DESCRIPTION_BASE_URL="https://provider-a.example.com/v1",
        DOCLING_PICTURE_DESCRIPTION_API_KEY="sk-test-key",
    )
    config_b = ConversionConfig(
        _env_file=None,
        DOCLING_PICTURE_DESCRIPTION_BASE_URL="https://provider-b.example.com/v1",
        DOCLING_PICTURE_DESCRIPTION_API_KEY="sk-test-key",
    )
    key_a = compute_idempotency_key(
        source_ref_hash="ref-rotate",
        converter_name="docling",
        config_snapshot=_docling_snapshot(config_a, picture_description_enabled=True),
    )
    key_b = compute_idempotency_key(
        source_ref_hash="ref-rotate",
        converter_name="docling",
        config_snapshot=_docling_snapshot(config_b, picture_description_enabled=True),
    )
    assert key_a == key_b


def test_compute_idempotency_key_stable_when_only_api_key_rotates():
    """Credentials are not output-affecting — rotating the api_key must not invalidate the key."""
    config_a = ConversionConfig(
        _env_file=None,
        DOCLING_PICTURE_DESCRIPTION_BASE_URL="https://provider.example.com/v1",
        DOCLING_PICTURE_DESCRIPTION_API_KEY="sk-original-key",
    )
    config_b = ConversionConfig(
        _env_file=None,
        DOCLING_PICTURE_DESCRIPTION_BASE_URL="https://provider.example.com/v1",
        DOCLING_PICTURE_DESCRIPTION_API_KEY="sk-rotated-key",
    )
    key_a = compute_idempotency_key(
        source_ref_hash="ref-rotate",
        converter_name="docling",
        config_snapshot=_docling_snapshot(config_a, picture_description_enabled=True),
    )
    key_b = compute_idempotency_key(
        source_ref_hash="ref-rotate",
        converter_name="docling",
        config_snapshot=_docling_snapshot(config_b, picture_description_enabled=True),
    )
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
