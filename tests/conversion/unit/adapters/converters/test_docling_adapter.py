"""Unit tests for the ``DoclingConverter`` adapter (Stage 3).

These tests are hermetic — no ``.env`` reads. Every ``ConversionConfig`` is
constructed with ``_env_file=None`` and explicit field overrides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aizk.conversion.adapters.converters import docling as docling_module
from aizk.conversion.adapters.converters.docling import DoclingConverter
from aizk.conversion.core.protocols import Converter
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.hashing import build_output_config_snapshot


def _make_disabled_config() -> ConversionConfig:
    """Config with picture-description provider NOT configured (disabled path)."""
    return ConversionConfig(
        _env_file=None,
        DOCLING_PICTURE_DESCRIPTION_BASE_URL="",
        DOCLING_PICTURE_DESCRIPTION_API_KEY="",
    )


def _make_enabled_config() -> ConversionConfig:
    """Config with picture-description provider fully configured (enabled path)."""
    return ConversionConfig(
        _env_file=None,
        DOCLING_PICTURE_DESCRIPTION_BASE_URL="https://provider.example.com/v1",
        DOCLING_PICTURE_DESCRIPTION_API_KEY="sk-test-key",
    )


def test_docling_supported_formats_contains_pdf_and_html() -> None:
    # Class-level inspection — must work without instantiating.
    assert ContentType.PDF in DoclingConverter.supported_formats
    assert ContentType.HTML in DoclingConverter.supported_formats
    assert isinstance(DoclingConverter.supported_formats, frozenset)


def test_docling_requires_gpu_is_true() -> None:
    # Class-level inspection — must work without instantiating.
    assert DoclingConverter.requires_gpu is True


def test_docling_satisfies_converter_protocol_structurally() -> None:
    config = _make_disabled_config()
    adapter = DoclingConverter(config)

    # ``Converter`` is NOT @runtime_checkable — probe required attributes
    # structurally on both the instance and the class.
    assert hasattr(adapter, "convert")
    assert callable(adapter.convert)
    assert hasattr(adapter, "config_snapshot")
    assert callable(adapter.config_snapshot)
    assert hasattr(DoclingConverter, "supported_formats")
    assert hasattr(DoclingConverter, "requires_gpu")

    # The Converter protocol annotates these class-level capability fields.
    # Confirm their expected types match the protocol's declared shape.
    assert isinstance(DoclingConverter.supported_formats, frozenset)
    assert isinstance(DoclingConverter.requires_gpu, bool)

    # And confirm the protocol itself declares those members (guard against
    # the protocol drifting without the adapter being updated).
    assert "convert" in dir(Converter)
    assert "config_snapshot" in dir(Converter)


def test_docling_config_snapshot_matches_legacy_field_set() -> None:
    config = _make_enabled_config()
    adapter = DoclingConverter(config)

    snapshot = adapter.config_snapshot()
    legacy_snapshot = build_output_config_snapshot(
        config,
        picture_description_enabled=config.is_picture_description_enabled(),
    )

    # Both the key SET and the values must match the legacy payload exactly.
    assert set(snapshot.keys()) == set(legacy_snapshot.keys())
    assert snapshot == legacy_snapshot


def test_docling_config_snapshot_excludes_endpoint_and_api_key() -> None:
    # Provider identity and credentials must never appear in the snapshot,
    # even when the provider is configured (they are not output-affecting).
    config = _make_enabled_config()
    snapshot = DoclingConverter(config).config_snapshot()

    assert "docling_picture_description_base_url" not in snapshot
    assert "docling_picture_description_api_key" not in snapshot


def test_docling_config_snapshot_includes_picture_description_enabled_flag() -> None:
    # Enabled fixture: flag is True and matches the derived helper.
    enabled_config = _make_enabled_config()
    enabled_snapshot = DoclingConverter(enabled_config).config_snapshot()
    assert "picture_description_enabled" in enabled_snapshot
    assert enabled_snapshot["picture_description_enabled"] is True
    assert enabled_snapshot["picture_description_enabled"] == enabled_config.is_picture_description_enabled()

    # Disabled fixture: flag is False and matches the derived helper.
    disabled_config = _make_disabled_config()
    disabled_snapshot = DoclingConverter(disabled_config).config_snapshot()
    assert "picture_description_enabled" in disabled_snapshot
    assert disabled_snapshot["picture_description_enabled"] is False
    assert disabled_snapshot["picture_description_enabled"] == disabled_config.is_picture_description_enabled()


def test_docling_config_snapshot_must_not_include_converter_name() -> None:
    # ``converter_name`` is the orchestrator's concern — the adapter's snapshot
    # MUST NOT include it (see tasks.md Stage 6b lines 129, 131).
    config = _make_enabled_config()
    snapshot = DoclingConverter(config).config_snapshot()
    assert "converter_name" not in snapshot


def test_docling_convert_dispatches_pdf_to_convert_pdf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _fake_convert_pdf(
        pdf_bytes: bytes,
        *,
        temp_dir: Path,
        config: ConversionConfig,
    ) -> tuple[str, list[Path]]:
        captured["pdf_bytes"] = pdf_bytes
        captured["temp_dir"] = temp_dir
        captured["config"] = config
        figure = tmp_path / "figure-001.png"
        return "# PDF markdown", [figure]

    monkeypatch.setattr(docling_module, "convert_pdf", _fake_convert_pdf)

    config = _make_disabled_config()
    adapter = DoclingConverter(config)
    artifacts = adapter.convert(ConversionInput(content=b"%PDF-1.4", content_type=ContentType.PDF))

    assert isinstance(artifacts, ConversionArtifacts)
    assert artifacts.markdown == "# PDF markdown"
    assert len(artifacts.figures) == 1
    assert artifacts.figures[0] == tmp_path / "figure-001.png"
    # Adapter populates docling_version in metadata (Stage 7).
    assert "docling_version" in artifacts.metadata

    # Verify the adapter passed the bytes, a temp_dir Path, and the config through.
    assert captured["pdf_bytes"] == b"%PDF-1.4"
    assert isinstance(captured["temp_dir"], Path)
    assert captured["config"] is config


def test_docling_convert_dispatches_html_to_convert_html(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _fake_convert_html(
        html_bytes: bytes,
        *,
        temp_dir: Path,
        config: ConversionConfig,
        source_url: str | None = None,
    ) -> tuple[str, list[Path]]:
        captured["html_bytes"] = html_bytes
        captured["temp_dir"] = temp_dir
        captured["config"] = config
        captured["source_url"] = source_url
        return "# HTML markdown", []

    monkeypatch.setattr(docling_module, "convert_html", _fake_convert_html)

    config = _make_disabled_config()
    adapter = DoclingConverter(config)
    artifacts = adapter.convert(
        ConversionInput(
            content=b"<html></html>",
            content_type=ContentType.HTML,
            metadata={"source_url": "https://example.com/p"},
        )
    )

    assert isinstance(artifacts, ConversionArtifacts)
    assert artifacts.markdown == "# HTML markdown"
    assert artifacts.figures == []
    # Adapter populates docling_version in metadata (Stage 7).
    assert "docling_version" in artifacts.metadata

    assert captured["html_bytes"] == b"<html></html>"
    assert isinstance(captured["temp_dir"], Path)
    assert captured["config"] is config
    assert captured["source_url"] == "https://example.com/p"


def test_docling_convert_html_without_source_url_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``source_url`` in metadata must translate to ``None`` downstream."""
    captured: dict[str, Any] = {}

    def _fake_convert_html(
        html_bytes: bytes,
        *,
        temp_dir: Path,
        config: ConversionConfig,
        source_url: str | None = None,
    ) -> tuple[str, list[Path]]:
        captured["source_url"] = source_url
        return "", []

    monkeypatch.setattr(docling_module, "convert_html", _fake_convert_html)

    config = _make_disabled_config()
    DoclingConverter(config).convert(ConversionInput(content=b"<html></html>", content_type=ContentType.HTML))

    assert captured["source_url"] is None


def test_docling_convert_raises_on_unsupported_content_type() -> None:
    config = _make_disabled_config()
    adapter = DoclingConverter(config)

    with pytest.raises(ValueError) as excinfo:
        adapter.convert(ConversionInput(content=b"a,b\n1,2", content_type=ContentType.CSV))

    # Error message should identify the unsupported type for operator debuggability.
    assert "csv" in str(excinfo.value).lower()
