"""Unit tests for the Docling converter worker.

Covers picture classification helpers, enrichment loop, the AnnotationPictureSerializer embedded in _docling_to_markdown,
and the trace-span wiring in convert_html.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from docling_core.types.doc.document import (
    DescriptionMetaField,
    PictureClassificationMetaField,
    PictureClassificationPrediction,
    PictureMeta,
    SummaryMetaField,
)

import aizk.conversion.processing.converter as converter_module
from aizk.conversion.processing.converter import (
    _ALT_TEXT_PROMPT,
    _enrich_picture_descriptions,
    _get_classification_label,
)
from aizk.conversion.utilities.config import DoclingConverterConfig

# ---------------------------------------------------------------------------
# Helpers to build a PictureItem-like mock with classification/description meta
# ---------------------------------------------------------------------------


def _make_classification_meta(*labels: str, confidence: float = 0.95) -> PictureClassificationMetaField:
    """Build a classification meta field; predictions are ordered as given."""
    return PictureClassificationMetaField(
        predictions=[
            PictureClassificationPrediction(class_name=label, confidence=confidence, created_by="test")
            for label in labels
        ]
    )


def _make_meta(
    *, labels: tuple[str, ...] = (), description: str | None = None, summary: str | None = None
) -> PictureMeta:
    """Build a ``PictureMeta`` with optional classification, description, and summary."""
    return PictureMeta(
        classification=_make_classification_meta(*labels) if labels else None,
        description=DescriptionMetaField(text=description, created_by="test") if description is not None else None,
        summary=SummaryMetaField(text=summary, created_by="test") if summary is not None else None,
    )


def _make_picture(meta: PictureMeta | None = None) -> MagicMock:
    pic = MagicMock()
    pic.self_ref = "#/pictures/0"
    pic.meta = meta
    return pic


# ---------------------------------------------------------------------------
# _get_classification_label
# ---------------------------------------------------------------------------


class TestGetClassificationLabel:
    def test_returns_top_label_when_classification_present(self):
        pic = _make_picture(_make_meta(labels=("bar_chart",)))
        assert _get_classification_label(pic) == "bar_chart"

    def test_returns_none_when_no_classification(self):
        pic = _make_picture(_make_meta(description="some description"))
        assert _get_classification_label(pic) is None

    def test_returns_none_when_meta_absent(self):
        pic = _make_picture(None)
        assert _get_classification_label(pic) is None

    def test_returns_first_class_when_multiple_classes(self):
        pic = _make_picture(_make_meta(labels=("pie_chart", "chart")))
        assert _get_classification_label(pic) == "pie_chart"

    def test_returns_none_when_predictions_empty(self):
        # The real PictureClassificationMetaField forbids empty predictions
        # (min_length=1), so the empty-predictions edge is modelled with a mock.
        meta = MagicMock()
        meta.classification.predictions = []
        pic = _make_picture(meta)
        assert _get_classification_label(pic) is None


# ---------------------------------------------------------------------------
# _enrich_picture_descriptions
# ---------------------------------------------------------------------------


def _make_config(
    *,
    base_url: str = "https://api.example.com",
    api_key: str = "test-key",
    model: str = "test-model",
    enable_classification: bool = True,
) -> DoclingConverterConfig:
    return DoclingConverterConfig(
        picture_description_base_url=base_url,
        picture_description_api_key=api_key,
        picture_description_model=model,
        picture_classification_enabled=enable_classification,
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

        pic = _make_picture(_make_meta(labels=("bar_chart",)))
        pic.get_image.return_value = MagicMock()

        doc = self._make_doc([pic])
        config = _make_config()

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        assert captured_prompts == ["<chart2summary>"]
        assert pic.meta.description.text == "chart description"

    def test_table_figure_uses_tables_html_prompt(self, monkeypatch):
        captured_prompts: list[str] = []

        def _fake_call_vlm(image, prompt, config):
            captured_prompts.append(prompt)
            return "table description"

        pic = _make_picture(_make_meta(labels=("table",)))
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

        pic = _make_picture(None)
        pic.get_image.return_value = MagicMock()

        doc = self._make_doc([pic])
        config = _make_config()

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        assert captured_prompts == [_ALT_TEXT_PROMPT]

    def test_description_written_to_meta(self, monkeypatch):
        def _fake_call_vlm(image, prompt, config):
            return "injected description"

        pic = _make_picture(None)
        pic.get_image.return_value = MagicMock()

        doc = self._make_doc([pic])
        config = _make_config()

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        # meta was absent; enrichment must create it and set the description.
        assert pic.meta.description.text == "injected description"

    def test_skips_when_description_disabled(self, monkeypatch):
        call_count = {"n": 0}

        def _fake_call_vlm(image, prompt, config):
            call_count["n"] += 1
            return "should not be called"

        pic = _make_picture(None)
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

        pic = _make_picture(None)
        pic.get_image.return_value = None  # no image

        doc = self._make_doc([pic])
        config = _make_config()

        monkeypatch.setattr(converter_module, "_call_vlm_api", _fake_call_vlm)
        monkeypatch.setattr(converter_module, "trace_model_call", _noop_trace)

        _enrich_picture_descriptions(doc, config)

        assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# AnnotationPictureSerializer with both annotations
# ---------------------------------------------------------------------------


class TestAnnotationPictureSerializer:
    """Tests for the AnnotationPictureSerializer embedded in _docling_to_markdown."""

    def _serialize_picture(self, meta: PictureMeta) -> str:
        """Build a minimal DoclingDocument with one picture and serialize it."""

        from docling_core.types.doc.document import DoclingDocument, PictureItem

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

        pic.meta = meta

        # Call _docling_to_markdown; suppress DoclingEmptyOutputError for empty docs
        try:
            result = converter_module._docling_to_markdown(doc)
        except converter_module.DoclingEmptyOutputError:
            result = ""
        return result

    def test_figure_type_comment_precedes_description_block(self):
        meta = _make_meta(labels=("bar_chart",), description="A bar chart showing sales data.")

        result = self._serialize_picture(meta)

        assert result, "Serializer produced empty output — figure meta was not rendered"
        type_pos = result.find("<!-- Figure Type: bar_chart -->")
        desc_pos = result.find("<!-- Figure Description -->")
        assert type_pos != -1, "Figure Type comment missing"
        assert desc_pos != -1, "Figure Description comment missing"
        assert type_pos < desc_pos, "Figure Type must precede Figure Description"

    def test_no_figure_type_comment_when_no_classification(self):
        result = self._serialize_picture(_make_meta(description="A generic figure."))

        assert "Figure Type" not in result

    def test_classification_and_description_meta_lines_are_suppressed(self):
        """The doc serializer must not emit bare ``[Classification]`` / ``[Description]``
        lines alongside our HTML comments.

        docling populates ``meta`` for every classified figure, which would
        otherwise trigger the framework's per-item meta rendering. The
        conversion-worker spec defines figure classification/description output
        as the HTML comments only, so those two framework lines must stay
        suppressed.
        """
        meta = _make_meta(labels=("bar_chart",), description="A bar chart.")

        result = self._serialize_picture(meta)

        assert "[Classification]" not in result
        assert "[Description]" not in result

    def test_other_meta_still_serializes(self):
        """Suppression is scoped to classification/description only.

        The block list must not drop other meta fields (e.g. ``summary``);
        those still render through the framework's default path so docling
        metadata we do not override is not silently lost.
        """
        meta = _make_meta(labels=("bar_chart",), description="A bar chart.", summary="A short summary.")

        result = self._serialize_picture(meta)

        # classification/description suppressed, but summary preserved.
        assert "[Classification]" not in result
        assert "[Description]" not in result
        assert "A short summary." in result


# ---------------------------------------------------------------------------
# convert_html trace-span wiring
# ---------------------------------------------------------------------------


class _FakeDocumentConverter:
    def convert(self, _source):
        return SimpleNamespace(document=SimpleNamespace(pictures=[]))


def _patch_convert_html_stubs(monkeypatch, trace_impl) -> None:
    """Wire the common fakes: no real Docling, no real markdown, no real figures."""
    monkeypatch.setattr(converter_module, "_create_document_converter", lambda *_a, **_kw: _FakeDocumentConverter())
    monkeypatch.setattr(converter_module, "_docling_to_markdown", lambda _doc: "markdown")
    monkeypatch.setattr(converter_module, "_extract_figures", lambda _doc, _out: [])
    monkeypatch.setattr(converter_module, "trace_model_call", trace_impl)


class TestConvertHtmlTracing:
    """Verify convert_html wires the `trace_model_call` span based on config."""

    def test_uses_llm_trace_when_picture_description_enabled(self, monkeypatch, tmp_path: Path) -> None:
        """When classification is enabled (default), trace comes from _enrich_picture_descriptions."""
        captured_calls: list[tuple[str, str, dict[str, object]]] = []

        @contextmanager
        def _capture_trace_model_call(*, name, span_type, attributes=None):
            captured_calls.append((name, span_type, attributes or {}))
            yield None

        _patch_convert_html_stubs(monkeypatch, _capture_trace_model_call)

        config = DoclingConverterConfig(
            _env_file=None,
            picture_description_base_url="https://openrouter.ai/api/v1",
            picture_description_api_key="test-key",
            picture_description_model="openai/gpt-5-nano",
        )
        markdown, figures, _ = converter_module.convert_html(b"<html></html>", temp_dir=tmp_path, config=config)

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

    def test_uses_builtin_trace_when_classification_disabled(self, monkeypatch, tmp_path: Path) -> None:
        """When classification is disabled, the trace wraps the Docling convert call with pipeline=html."""
        captured_calls: list[tuple[str, str, dict[str, object]]] = []

        @contextmanager
        def _capture_trace_model_call(*, name, span_type, attributes=None):
            captured_calls.append((name, span_type, attributes or {}))
            yield None

        _patch_convert_html_stubs(monkeypatch, _capture_trace_model_call)

        config = DoclingConverterConfig(
            _env_file=None,
            picture_description_base_url="https://openrouter.ai/api/v1",
            picture_description_api_key="test-key",
            picture_description_model="openai/gpt-5-nano",
            picture_classification_enabled=False,
        )
        markdown, figures, _ = converter_module.convert_html(b"<html></html>", temp_dir=tmp_path, config=config)

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

    def test_skips_llm_trace_when_picture_description_disabled(self, monkeypatch, tmp_path: Path) -> None:
        trace_calls = {"count": 0}

        @contextmanager
        def _capture_trace_model_call(**_kwargs):
            trace_calls["count"] += 1
            yield None

        _patch_convert_html_stubs(monkeypatch, _capture_trace_model_call)

        config = DoclingConverterConfig(
            _env_file=None,
            picture_description_base_url="",
            picture_description_api_key="",
        )
        markdown, figures, _ = converter_module.convert_html(b"<html></html>", temp_dir=tmp_path, config=config)

        assert markdown == "markdown"
        assert figures == []
        assert trace_calls["count"] == 0


# ---------------------------------------------------------------------------
# EgressPolicyError must propagate through convert_html / convert_pdf unchanged
# ---------------------------------------------------------------------------


class TestEgressErrorPropagation:
    """Verify ``EgressPolicyError`` subclasses propagate through ``convert_html`` and
    ``convert_pdf`` without being repackaged as :class:`DoclingError`.

    The broad ``except Exception`` arms in those functions exist to map arbitrary
    Docling failures to a typed ``DoclingError`` for the orchestrator. ``DoclingError``
    is classified as ``retryable=True`` and its ``error_code='docling_error'`` is NOT
    in the orchestrator's ``_EGRESS_POLICY_ERROR_CODES`` sanitization filter — so
    swallowing a ``WorkspaceEscape`` (or any sibling) here would (a) cause the job
    to be retried forever against an attacker probe, and (b) leak the rejected path
    into the persisted ``error_message`` via ``str(error)``.
    """

    def test_convert_html_does_not_wrap_workspace_escape_as_docling_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A ``WorkspaceEscape`` raised during ``converter.convert(...)`` must propagate untouched."""
        from aizk.conversion.core.errors import WorkspaceEscape

        rejected_path = "/etc/ssh/ssh_host_rsa_key"

        class _EscapingConverter:
            def convert(self, _source):
                raise WorkspaceEscape(f"Path {rejected_path} escapes workspace")

        monkeypatch.setattr(converter_module, "_create_document_converter", lambda *_a, **_kw: _EscapingConverter())
        monkeypatch.setattr(converter_module, "_docling_to_markdown", lambda _doc: "")
        monkeypatch.setattr(converter_module, "_extract_figures", lambda _doc, _out: [])

        config = DoclingConverterConfig(_env_file=None)

        with pytest.raises(WorkspaceEscape) as excinfo:
            converter_module.convert_html(b"<html></html>", temp_dir=tmp_path, config=config)

        # Must be the original exception, not a DoclingError wrapping it.
        assert not isinstance(excinfo.value, converter_module.DoclingError)
        assert rejected_path in str(excinfo.value)

    def test_convert_html_does_not_wrap_egress_error_from_prefetch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An ``EgressPolicyError`` raised by ``prefetch_images`` must propagate."""
        from aizk.conversion.core.errors import DenyListDestination

        rejected_host = "169.254.169.254"

        async def _raising_prefetch(_html, _ws, **_kwargs):
            raise DenyListDestination(f"Resolved address for host {rejected_host!r} is in deny set")

        monkeypatch.setattr(converter_module, "prefetch_images", _raising_prefetch)
        monkeypatch.setattr(
            converter_module, "_create_document_converter", lambda *_a, **_kw: _FakeDocumentConverter()
        )
        monkeypatch.setattr(converter_module, "_docling_to_markdown", lambda _doc: "")
        monkeypatch.setattr(converter_module, "_extract_figures", lambda _doc, _out: [])

        config = DoclingConverterConfig(_env_file=None)

        with pytest.raises(DenyListDestination) as excinfo:
            converter_module.convert_html(b"<html></html>", temp_dir=tmp_path, config=config)

        assert not isinstance(excinfo.value, converter_module.DoclingError)
        assert rejected_host in str(excinfo.value)

    def test_convert_pdf_does_not_wrap_workspace_escape_as_docling_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Symmetry: ``convert_pdf`` must also re-raise ``EgressPolicyError`` subclasses."""
        from aizk.conversion.core.errors import WorkspaceEscape

        rejected_path = "/etc/passwd"

        class _EscapingConverter:
            def convert(self, _source):
                raise WorkspaceEscape(f"Path {rejected_path} escapes workspace")

        monkeypatch.setattr(converter_module, "_create_document_converter", lambda *_a, **_kw: _EscapingConverter())
        monkeypatch.setattr(converter_module, "_docling_to_markdown", lambda _doc: "")
        monkeypatch.setattr(converter_module, "_extract_figures", lambda _doc, _out: [])

        config = DoclingConverterConfig(_env_file=None)

        with pytest.raises(WorkspaceEscape) as excinfo:
            converter_module.convert_pdf(b"%PDF-1.4 fake", temp_dir=tmp_path, config=config)

        assert not isinstance(excinfo.value, converter_module.DoclingError)
        assert rejected_path in str(excinfo.value)


def test_convert_html_threads_source_url_and_policy_into_the_admission_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``convert_html`` must hand its ``source_url`` and policy to ``prefetch_images``.

    Resolution of page-relative references happens inside the admission step, so
    dropping either keyword at this call site would silently sever it: every
    relative image would fail egress validation as written and be dropped, while
    the admission step's own tests stay green because they receive the
    parameters directly.
    """
    from aizk.conversion.utilities.html_prefetch import PrefetchPolicy

    captured: dict[str, object] = {}

    async def _capture_prefetch(html, _workspace, *, policy=None, source_url=None):
        captured["policy"] = policy
        captured["source_url"] = source_url
        return html

    monkeypatch.setattr(converter_module, "prefetch_images", _capture_prefetch)
    monkeypatch.setattr(converter_module, "_create_document_converter", lambda *_a, **_kw: _FakeDocumentConverter())
    monkeypatch.setattr(converter_module, "_docling_to_markdown", lambda _doc: "markdown")
    monkeypatch.setattr(converter_module, "_extract_figures", lambda _doc, _out: [])

    config = DoclingConverterConfig(_env_file=None)
    policy = PrefetchPolicy(per_image_max_bytes=1234)

    converter_module.convert_html(
        b"<html></html>",
        temp_dir=tmp_path,
        config=config,
        source_url="https://origin.example/article",
        prefetch_policy=policy,
    )

    assert captured["source_url"] == "https://origin.example/article"
    assert captured["policy"] is policy


class TestConverterDereferenceCapability:
    """The converter must be configured so it can dereference no location in the document.

    Admission upstream covers ``<img src>``; these flags are what cover every
    other resource-bearing shape a page might use, so they are contract rather
    than incidental configuration.
    """

    @staticmethod
    def _html_backend_options(**kwargs):
        """Build the converter and return the HTML backend options it was given."""
        from docling.datamodel.base_models import InputFormat

        config = DoclingConverterConfig(_env_file=None)
        converter = converter_module._create_document_converter(config, **kwargs)
        return converter.format_to_options[InputFormat.HTML].backend_options

    def test_no_fetch_lever_is_left_open(self) -> None:
        """Remote fetch, local fetch, and browser rendering are all off.

        Browser rendering is a separate lever from the two fetch flags: its
        request filter permits the file scheme outright and never consults
        ``enable_local_fetch``, so leaving it defaulted would reopen local reads
        through a different door.
        """
        options = self._html_backend_options()

        assert options.enable_remote_fetch is False
        assert options.enable_local_fetch is False
        assert options.render_page is False

    def test_base64_ceiling_is_derived_from_the_admission_cap(self) -> None:
        """The converter's per-image ceiling comes from the pre-fetch policy."""
        from aizk.conversion.utilities.html_prefetch import PrefetchPolicy

        policy = PrefetchPolicy()
        options = self._html_backend_options(prefetch_policy=policy)

        assert options.max_image_data_base64_bytes == policy.per_image_max_bytes

    def test_raising_the_admission_cap_raises_the_converter_ceiling_with_it(self) -> None:
        """The two caps measure the same quantity and must not be able to disagree.

        Left at the library default, an operator raising the admission cap past it
        would pass the pipeline's own limit only to be refused by the converter,
        and the refusal surfaces as a warning rather than an error.
        """
        from aizk.conversion.utilities.html_prefetch import PrefetchPolicy

        raised = PrefetchPolicy(per_image_max_bytes=64 * 1024 * 1024)
        options = self._html_backend_options(prefetch_policy=raised)

        assert options.max_image_data_base64_bytes == raised.per_image_max_bytes

    def test_page_authored_data_image_is_held_to_the_admission_cap(self, tmp_path: Path) -> None:
        """A `data:` image is admitted without a fetch, so only this ceiling bounds its size.

        Without the derived ceiling a page could inline an image larger than the
        per-image cap and bypass the limit entirely.
        """
        import base64
        from io import BytesIO

        from PIL import Image

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.document import InputDocument
        from docling_core.types.doc.document import PictureItem

        from aizk.conversion.utilities.html_prefetch import PrefetchPolicy

        buf = BytesIO()
        Image.new("RGB", (64, 64), color=(0, 128, 255)).save(buf, format="PNG")
        payload = buf.getvalue()

        # A cap below the payload size must refuse it; a cap above must admit it.
        def _convert_with_cap(cap: int):
            options = self._html_backend_options(prefetch_policy=PrefetchPolicy(per_image_max_bytes=cap))
            encoded = base64.b64encode(payload).decode()
            html = f'<html><body><img src="data:image/png;base64,{encoded}"></body></html>'
            in_doc = InputDocument(
                path_or_stream=BytesIO(html.encode()),
                format=InputFormat.HTML,
                filename="document.html",
                backend=converter_module.HTMLDocumentBackend,
                backend_options=options,
            )
            assert in_doc._backend is not None
            doc = in_doc._backend.convert()
            return [item for item, _ in doc.iterate_items() if isinstance(item, PictureItem)]

        under_cap = _convert_with_cap(len(payload) - 1)
        assert under_cap[0].image is None, "an oversized inline image must not be admitted"

        over_cap = _convert_with_cap(len(payload) + 1)
        assert over_cap[0].image is not None, "an inline image within the cap must be admitted"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _noop_trace(*, name, span_type, attributes=None):
    yield None
