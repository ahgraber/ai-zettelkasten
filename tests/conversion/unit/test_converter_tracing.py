from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from aizk.conversion.utilities.config import ConversionConfig
import aizk.conversion.workers.converter as converter


class _FakeDocumentConverter:
    def convert(self, _source):
        return SimpleNamespace(document=SimpleNamespace(pictures=[]))


def test_convert_html_uses_llm_trace_when_picture_description_enabled(monkeypatch, tmp_path: Path) -> None:
    """When classification is enabled (default), trace comes from _enrich_picture_descriptions."""
    captured_calls: list[tuple[str, str, dict[str, object]]] = []

    @contextmanager
    def _capture_trace_model_call(*, name, span_type, attributes=None):
        captured_calls.append((name, span_type, attributes or {}))
        yield None

    monkeypatch.setattr(converter, "_create_document_converter", lambda *_args, **_kwargs: _FakeDocumentConverter())
    monkeypatch.setattr(converter, "_docling_to_markdown", lambda _doc: "markdown")
    monkeypatch.setattr(converter, "_extract_figures", lambda _doc, _out: [])
    monkeypatch.setattr(converter, "trace_model_call", _capture_trace_model_call)

    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_API_KEY", "test-key")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_MODEL", "openai/gpt-5-nano")
    # Classification enabled by default; enrichment loop owns the trace span
    config = ConversionConfig(_env_file=None)
    markdown, figures = converter.convert_html(b"<html></html>", temp_dir=tmp_path, config=config)

    assert markdown == "markdown"
    assert figures == []
    assert captured_calls == [
        (
            "llm.chat.completions.docling_picture_description",
            "CHAT_MODEL",
            {
                "model": "openai/gpt-5-nano",
                "pipeline": "enrichment",
                "provider_endpoint": "/chat/completions",
            },
        )
    ]


def test_convert_html_uses_builtin_trace_when_classification_disabled(monkeypatch, tmp_path: Path) -> None:
    """When classification is disabled, the trace wraps the Docling convert call with pipeline=html."""
    captured_calls: list[tuple[str, str, dict[str, object]]] = []

    @contextmanager
    def _capture_trace_model_call(*, name, span_type, attributes=None):
        captured_calls.append((name, span_type, attributes or {}))
        yield None

    monkeypatch.setattr(converter, "_create_document_converter", lambda *_args, **_kwargs: _FakeDocumentConverter())
    monkeypatch.setattr(converter, "_docling_to_markdown", lambda _doc: "markdown")
    monkeypatch.setattr(converter, "_extract_figures", lambda _doc, _out: [])
    monkeypatch.setattr(converter, "trace_model_call", _capture_trace_model_call)

    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_API_KEY", "test-key")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_MODEL", "openai/gpt-5-nano")
    monkeypatch.setenv("DOCLING_ENABLE_PICTURE_CLASSIFICATION", "false")
    config = ConversionConfig(_env_file=None)
    markdown, figures = converter.convert_html(b"<html></html>", temp_dir=tmp_path, config=config)

    assert markdown == "markdown"
    assert figures == []
    assert captured_calls == [
        (
            "llm.chat.completions.docling_picture_description",
            "CHAT_MODEL",
            {
                "model": "openai/gpt-5-nano",
                "pipeline": "html",
                "provider_endpoint": "/chat/completions",
            },
        )
    ]


def test_convert_html_skips_llm_trace_when_picture_description_disabled(monkeypatch, tmp_path: Path) -> None:
    trace_calls = {"count": 0}

    @contextmanager
    def _capture_trace_model_call(**_kwargs):
        trace_calls["count"] += 1
        yield None

    monkeypatch.setattr(converter, "_create_document_converter", lambda *_args, **_kwargs: _FakeDocumentConverter())
    monkeypatch.setattr(converter, "_docling_to_markdown", lambda _doc: "markdown")
    monkeypatch.setattr(converter, "_extract_figures", lambda _doc, _out: [])
    monkeypatch.setattr(converter, "trace_model_call", _capture_trace_model_call)

    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION_API_KEY", "")
    config = ConversionConfig(_env_file=None)
    markdown, figures = converter.convert_html(b"<html></html>", temp_dir=tmp_path, config=config)

    assert markdown == "markdown"
    assert figures == []
    assert trace_calls["count"] == 0
