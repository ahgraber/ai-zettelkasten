"""Tests for DoclingConverter adapter: class-level attributes, config_snapshot, import path."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from aizk.conversion.adapters.converters.docling import DoclingConverter
from aizk.conversion.core.types import ContentType
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import build_output_config_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> ConversionConfig:
    defaults = {
        "DOCLING_PICTURE_DESCRIPTION_BASE_URL": "https://api.example.com",
        "DOCLING_PICTURE_DESCRIPTION_API_KEY": "test-key",
        "_env_file": None,
    }
    defaults.update(overrides)
    return ConversionConfig(**defaults)


def _make_config_disabled() -> ConversionConfig:
    return ConversionConfig(
        DOCLING_PICTURE_DESCRIPTION_BASE_URL="",
        DOCLING_PICTURE_DESCRIPTION_API_KEY="",
        _env_file=None,
    )


# ---------------------------------------------------------------------------
# Class-attribute tests (inspectable without instantiation)
# ---------------------------------------------------------------------------

def test_docling_converter_supported_formats_contains_pdf_and_html():
    assert ContentType.PDF in DoclingConverter.supported_formats
    assert ContentType.HTML in DoclingConverter.supported_formats


def test_docling_converter_supported_formats_is_frozenset():
    assert isinstance(DoclingConverter.supported_formats, frozenset)


def test_docling_converter_requires_gpu_is_true():
    assert DoclingConverter.requires_gpu is True


def test_docling_converter_class_attrs_inspectable_without_instantiation():
    assert hasattr(DoclingConverter, "supported_formats")
    assert hasattr(DoclingConverter, "requires_gpu")


# ---------------------------------------------------------------------------
# config_snapshot field-set tests
# ---------------------------------------------------------------------------

def test_config_snapshot_field_set_matches_build_output_config_snapshot_with_description():
    """config_snapshot() keys must match build_output_config_snapshot when description is active."""
    config = _make_config()
    converter = DoclingConverter(config)
    snapshot = converter.config_snapshot()

    expected = build_output_config_snapshot(
        config,
        picture_description_enabled=config.is_picture_description_enabled(),
    )
    assert set(snapshot.keys()) == set(expected.keys())


def test_config_snapshot_field_set_matches_build_output_config_snapshot_without_description():
    """config_snapshot() keys must match build_output_config_snapshot when description is disabled."""
    config = _make_config_disabled()
    converter = DoclingConverter(config)
    snapshot = converter.config_snapshot()

    expected = build_output_config_snapshot(
        config,
        picture_description_enabled=False,
    )
    assert set(snapshot.keys()) == set(expected.keys())


def test_config_snapshot_values_match_expected():
    """config_snapshot() values must equal those from build_output_config_snapshot."""
    config = _make_config()
    converter = DoclingConverter(config)
    snapshot = converter.config_snapshot()

    expected = build_output_config_snapshot(
        config,
        picture_description_enabled=config.is_picture_description_enabled(),
    )
    assert snapshot == expected


def test_config_snapshot_excludes_api_credentials():
    """config_snapshot() must not leak endpoint URL or API key."""
    config = _make_config()
    converter = DoclingConverter(config)
    snapshot = converter.config_snapshot()

    assert "docling_picture_description_base_url" not in snapshot
    assert "docling_picture_description_api_key" not in snapshot


def test_config_snapshot_picture_description_enabled_reflects_config():
    converter_on = DoclingConverter(_make_config())
    converter_off = DoclingConverter(_make_config_disabled())

    assert converter_on.config_snapshot()["picture_description_enabled"] is True
    assert converter_off.config_snapshot()["picture_description_enabled"] is False


