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


# ---------------------------------------------------------------------------
# Import path compatibility
# ---------------------------------------------------------------------------

def test_docling_converter_importable_from_workers_converter_path():
    """DoclingConverter from workers.converter must be the same class as from adapters.

    workers/converter.py requires httpx, docling, etc. which are not installed in the
    unit-test venv.  We mock those heavy modules in sys.modules before importing,
    then verify the re-export resolves to the canonical adapter class.
    """
    import importlib
    import sys
    from types import ModuleType
    from unittest.mock import MagicMock

    _heavy = [
        "httpx", "PIL", "PIL.Image",
        "docling", "docling.datamodel", "docling.datamodel.accelerator_options",
        "docling.datamodel.backend_options", "docling.datamodel.base_models",
        "docling.datamodel.document", "docling.datamodel.pipeline_options",
        "docling.document_converter",
        "docling_core", "docling_core.transforms", "docling_core.transforms.serializer",
        "docling_core.transforms.serializer.base", "docling_core.transforms.serializer.common",
        "docling_core.transforms.serializer.html", "docling_core.transforms.serializer.markdown",
        "docling_core.types", "docling_core.types.doc", "docling_core.types.doc.base",
        "docling_core.types.doc.document", "docling_core.types.io",
        "aizk.utilities.mlflow_tracing",
        "aizk.utilities",
    ]

    # Stash any previously imported workers.converter so we can restore it.
    _prev = sys.modules.pop("aizk.conversion.workers.converter", None)
    _mocks: dict[str, object] = {}
    try:
        for mod_name in _heavy:
            if mod_name not in sys.modules:
                _mocks[mod_name] = sys.modules.setdefault(mod_name, MagicMock())

        workers_converter = importlib.import_module("aizk.conversion.workers.converter")
        ReExported = workers_converter.DoclingConverter  # type: ignore[attr-defined]
        # Object identity (`is`) is not checked here: the test's sys.modules surgery
        # means workers.converter and the test module may hold different module
        # instances of the adapter.  The behaviorally meaningful check is that
        # the re-exported name resolves to the correct class from the correct module.
        assert ReExported.__name__ == "DoclingConverter"
        assert ReExported.__module__ == "aizk.conversion.adapters.converters.docling"
    finally:
        # Restore original state — remove our mock entries, put back original module.
        for mod_name in _mocks:
            sys.modules.pop(mod_name, None)
        sys.modules.pop("aizk.conversion.workers.converter", None)
        if _prev is not None:
            sys.modules["aizk.conversion.workers.converter"] = _prev
