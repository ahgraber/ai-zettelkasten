"""Unit tests for picture classification helpers and enrichment loop."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from aizk.conversion.utilities.config import ConversionConfig
import aizk.conversion.workers.converter as converter_module
from aizk.conversion.workers.converter import (
    _ALT_TEXT_PROMPT,
    _LABEL_TO_PROMPT,
    _enrich_picture_descriptions,
    _get_classification_label,
)

# ---------------------------------------------------------------------------
# Helpers to build mock PictureItem with annotations
# ---------------------------------------------------------------------------


def _make_classification_annotation(label: str, confidence: float = 0.95) -> MagicMock:
    ann = MagicMock()
    ann.__class__ = converter_module.PictureClassificationData
    predicted_class = MagicMock()
    predicted_class.class_name = label
    predicted_class.confidence = confidence
    ann.predicted_classes = [predicted_class]
    return ann


def _make_description_annotation(text: str) -> MagicMock:
    ann = MagicMock()
    ann.__class__ = converter_module.PictureDescriptionData
    ann.text = text
    return ann


def _make_picture(annotations: list) -> MagicMock:
    pic = MagicMock()
    pic.self_ref = "#/pictures/0"
    pic.annotations = list(annotations)
    return pic


# ---------------------------------------------------------------------------
# Task 7.3: _get_classification_label
# ---------------------------------------------------------------------------


class TestGetClassificationLabel:
    def test_returns_top_label_when_classification_present(self):
        ann = _make_classification_annotation("bar_chart")
        pic = _make_picture([ann])
        assert _get_classification_label(pic) == "bar_chart"

    def test_returns_none_when_no_classification_annotation(self):
        ann = _make_description_annotation("some description")
        pic = _make_picture([ann])
        assert _get_classification_label(pic) is None

    def test_returns_none_for_empty_annotations(self):
        pic = _make_picture([])
        assert _get_classification_label(pic) is None

    def test_returns_first_class_when_multiple_classes(self):
        ann = MagicMock()
        ann.__class__ = converter_module.PictureClassificationData
        cls1 = MagicMock()
        cls1.class_name = "pie_chart"
        cls2 = MagicMock()
        cls2.class_name = "chart"
        ann.predicted_classes = [cls1, cls2]
        pic = _make_picture([ann])
        assert _get_classification_label(pic) == "pie_chart"

    def test_returns_none_when_predicted_classes_empty(self):
        ann = MagicMock()
        ann.__class__ = converter_module.PictureClassificationData
        ann.predicted_classes = []
        pic = _make_picture([ann])
        assert _get_classification_label(pic) is None


# ---------------------------------------------------------------------------
# Task 7.4: _enrich_picture_descriptions
# ---------------------------------------------------------------------------


def _make_config(
    *,
    base_url: str = "https://api.example.com",
    api_key: str = "test-key",
    model: str = "test-model",
    enable_classification: bool = True,
) -> ConversionConfig:
    return ConversionConfig(
        DOCLING_PICTURE_DESCRIPTION_BASE_URL=base_url,
        DOCLING_PICTURE_DESCRIPTION_API_KEY=api_key,
        DOCLING_PICTURE_DESCRIPTION_MODEL=model,
        DOCLING_ENABLE_PICTURE_CLASSIFICATION=enable_classification,
        _env_file=None,
    )


class TestEnrichPictureDescriptions:
    def _make_doc(self, pictures: list) -> MagicMock:
        doc = MagicMock()
        doc.pictures = pictures
        return doc

    def test_chart_figure_uses_chart2summary_prompt(self, monkeypatch):
        captured_prompts: list[str] = []

        def _fake_call_vlm(image, prompt, config):
            captured_prompts.append(prompt)
            return "chart description"

        ann = _make_classification_annotation("bar_chart")
        pic = _make_picture([ann])
        pic.get_image.return_value = MagicMock()

        doc = self._make_doc([pic])
        config = _make_config()

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        assert captured_prompts == ["<chart2summary>"]
        assert any(
            isinstance(a, converter_module.PictureDescriptionData)
            or (hasattr(a, "text") and a.text == "chart description")
            for a in pic.annotations
        )

    def test_table_figure_uses_tables_html_prompt(self, monkeypatch):
        captured_prompts: list[str] = []

        def _fake_call_vlm(image, prompt, config):
            captured_prompts.append(prompt)
            return "table description"

        ann = _make_classification_annotation("table")
        pic = _make_picture([ann])
        pic.get_image.return_value = MagicMock()

        doc = self._make_doc([pic])
        config = _make_config()

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        assert captured_prompts == ["<tables_html>"]

    def test_unclassified_figure_uses_generic_prompt(self, monkeypatch):
        captured_prompts: list[str] = []

        def _fake_call_vlm(image, prompt, config):
            captured_prompts.append(prompt)
            return "generic description"

        pic = _make_picture([])
        pic.get_image.return_value = MagicMock()

        doc = self._make_doc([pic])
        config = _make_config()

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        assert captured_prompts == [_ALT_TEXT_PROMPT]

    def test_picture_description_data_appended(self, monkeypatch):
        def _fake_call_vlm(image, prompt, config):
            return "injected description"

        pic = _make_picture([])
        pic.get_image.return_value = MagicMock()

        doc = self._make_doc([pic])
        config = _make_config()

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        appended = pic.annotations[-1]
        assert appended.text == "injected description"

    def test_skips_when_description_disabled(self, monkeypatch):
        call_count = {"n": 0}

        def _fake_call_vlm(image, prompt, config):
            call_count["n"] += 1
            return "should not be called"

        pic = _make_picture([])
        pic.get_image.return_value = MagicMock()
        doc = self._make_doc([pic])

        config = _make_config(base_url="", api_key="")

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        assert call_count["n"] == 0

    def test_skips_figure_without_image(self, monkeypatch):
        call_count = {"n": 0}

        def _fake_call_vlm(image, prompt, config):
            call_count["n"] += 1
            return "should not be called"

        pic = _make_picture([])
        pic.get_image.return_value = None  # no image

        doc = self._make_doc([pic])
        config = _make_config()

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# Task 7.5: AnnotationPictureSerializer with both annotations
# ---------------------------------------------------------------------------


class TestAnnotationPictureSerializer:
    """Tests for the AnnotationPictureSerializer embedded in _docling_to_markdown."""

    def _serialize_picture(self, annotations: list) -> str:
        """Build a minimal DoclingDocument with one picture and serialize it."""
        import json

        from docling_core.transforms.serializer.html import HTMLTableSerializer
        from docling_core.transforms.serializer.markdown import MarkdownDocSerializer, MarkdownParams
        from docling_core.types.doc.base import ImageRefMode as IRMode
        from docling_core.types.doc.document import DoclingDocument, ImageRefMode, PictureItem

        # Build the minimal document JSON that docling_core can parse
        doc_dict = {
            "schema_name": "DoclingDocument",
            "version": "1.0.0",
            "name": "test",
            "body": {
                "self_ref": "#/body",
                "children": [{"$ref": "#/pictures/0"}],
                "name": "__root__",
                "label": "unspecified",
            },
            "pictures": [
                {
                    "self_ref": "#/pictures/0",
                    "parent": {"$ref": "#/body"},
                    "label": "picture",
                    "captions": [],
                    "references": [],
                    "footnotes": [],
                    "annotations": [],
                    "image": None,
                }
            ],
        }
        doc = DoclingDocument.model_validate(doc_dict)
        pic: PictureItem = doc.pictures[0]

        for ann in annotations:
            pic.annotations.append(ann)

        # Call _docling_to_markdown; suppress DoclingEmptyOutputError for empty docs
        try:
            result = converter_module._docling_to_markdown(doc)
        except converter_module.DoclingEmptyOutputError:
            result = ""
        return result

    def test_figure_type_comment_precedes_description_block(self):
        from docling_core.types.doc.document import PictureClassificationClass, PictureClassificationData as RealPCD

        cls = PictureClassificationClass(class_name="bar_chart", confidence=0.9)
        classification = RealPCD(provenance="test", predicted_classes=[cls])
        description = converter_module.PictureDescriptionData(
            text="A bar chart showing sales data.", provenance="test"
        )

        result = self._serialize_picture([classification, description])

        assert result, "Serializer produced empty output — annotations were not rendered"
        type_pos = result.find("<!-- Figure Type: bar_chart -->")
        desc_pos = result.find("<!-- Figure Description -->")
        assert type_pos != -1, "Figure Type comment missing"
        assert desc_pos != -1, "Figure Description comment missing"
        assert type_pos < desc_pos, "Figure Type must precede Figure Description"

    def test_no_figure_type_comment_when_no_classification(self):
        description = converter_module.PictureDescriptionData(text="A generic figure.", provenance="test")

        result = self._serialize_picture([description])

        assert "Figure Type" not in result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _noop_trace(*, name, span_type, attributes=None):
    yield None
