"""Docling conversion utilities for HTML and PDF content."""

from __future__ import annotations

import base64
from io import BytesIO
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from PIL import Image
from pydantic import AnyUrl, HttpUrl

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.backend_options import HTMLBackendOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    ConvertPipelineOptions,
    EasyOcrOptions,
    PictureDescriptionApiOptions,
    ThreadedPdfPipelineOptions,
)
from docling.document_converter import (
    DocumentConverter,
    HTMLFormatOption,
    MarkdownFormatOption,
    PdfFormatOption,
)
from docling_core.transforms.serializer.base import BaseDocSerializer, SerializationResult
from docling_core.transforms.serializer.common import _should_use_legacy_annotations, create_ser_result
from docling_core.transforms.serializer.html import HTMLTableSerializer
from docling_core.transforms.serializer.markdown import (
    MarkdownDocSerializer,
    MarkdownParams,
    MarkdownPictureSerializer,
)
from docling_core.types.doc.base import ImageRefMode
from docling_core.types.doc.document import (
    DoclingDocument,
    PictureClassificationData,
    PictureDescriptionData,
    PictureItem,
)
from docling_core.types.io import DocumentStream

from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.paths import figure_dir
from aizk.utilities.mlflow_tracing import trace_model_call

logger = logging.getLogger(__name__)

# Generic alt-text prompt used for figures without a recognized classifier label.
_ALT_TEXT_PROMPT = """
Provide a detailed description for this figure that captures the main subject, action, and any critical visual information including key components, relationships, and outcomes shown with the goal that someone who reads the description would be able to redraw the figure. Prioritize detail over brevity.

- Do not rely on phrases like "shown above" or "as seen"
- Do not say "image of," "picture of," "figure of," or "graphic of", etc.
- Do not describe purely visual styling unless meaningful
- Do not guess emotions, identities, or intent unless clearly conveyed

Respond ONLY with the description, no other text.
""".strip()

# Label-to-prompt routing table for DocumentFigureClassifier v2.5.
# Label vocabulary is empirically confirmed; unknown labels fall back to _ALT_TEXT_PROMPT.
# Log unknown labels at DEBUG so the actual vocabulary can be confirmed during deployment.
_LABEL_TO_PROMPT: dict[str, str] = {
    # Chart-type labels
    "chart": "<chart2summary>",
    "bar_chart": "<chart2summary>",
    "line_chart": "<chart2summary>",
    "pie_chart": "<chart2summary>",
    "scatter_chart": "<chart2summary>",
    "area_chart": "<chart2summary>",
    # Table-type labels
    "table": "<tables_html>",
}


class ConversionError(Exception):
    """Base exception for conversion errors.

    Contract: Exceptions used by the conversion pipeline MUST declare
    their retry semantics via the `retryable` attribute.

    - retryable=True: transient failures that should be retried
    - retryable=False: permanent failures that should not be retried
    """

    # Default to retryable for conversion errors unless specified otherwise.
    retryable = True

    def __init__(self, message: str, error_code: str):
        super().__init__(message)
        self.error_code = error_code


class DoclingError(ConversionError):
    """Raised when Docling conversion fails."""

    def __init__(self, message: str):
        super().__init__(f"Docling conversion failed: {message}", "docling_error")


class DoclingEmptyOutputError(ConversionError):
    """Raised when Docling produces no Markdown content."""

    def __init__(self):
        super().__init__("Docling produced no Markdown content", "docling_empty_output")
        # Empty output indicates a permanent failure for this input.
        self.retryable = False


def _get_picture_description_options(config: ConversionConfig) -> Optional[PictureDescriptionApiOptions]:
    """Build picture description options from environment.

    Uses OpenRouter-compatible endpoint if configured.
    """
    base_url = config.chat_completions_base_url.rstrip("/")
    api_key = config.chat_completions_api_key

    if not base_url or not api_key:
        logger.warning("Picture description disabled: CHAT_COMPLETIONS_BASE_URL or CHAT_COMPLETIONS_API_KEY not set")
        return None

    model_name = config.docling_vlm_model
    timeout = float(config.docling_picture_timeout)

    return PictureDescriptionApiOptions(
        url=HttpUrl(f"{base_url}/chat/completions"),
        params={
            "model": model_name,
            "seed": 42,
            "reasoning_effort": "low",
        },
        headers={"Authorization": f"Bearer {api_key}"},
        prompt=_ALT_TEXT_PROMPT,
        timeout=timeout,
    )


def _create_document_converter(
    config: ConversionConfig,
    source_url: Optional[str] = None,
) -> DocumentConverter:
    """Create a DocumentConverter with HTML and PDF format options."""
    picture_opts = _get_picture_description_options(config)

    # Two-phase approach: when classification is enabled and VLM is configured,
    # suppress Docling's built-in description; the enrichment loop handles it instead.
    classification_active = config.docling_enable_picture_classification and bool(picture_opts)
    builtin_description_active = bool(picture_opts) and not classification_active

    # HTML format options
    html_pipeline_opts = ConvertPipelineOptions()
    html_pipeline_opts.enable_remote_services = bool(picture_opts)
    if builtin_description_active:
        html_pipeline_opts.do_picture_description = True
        html_pipeline_opts.picture_description_options = picture_opts
    else:
        html_pipeline_opts.do_picture_description = False

    html_backend_opts = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=True,
        enable_local_fetch=True,
        source_uri=AnyUrl(source_url) if source_url else None,
        add_title=True,  # default
        infer_furniture=True,  # default
    )

    html_format = HTMLFormatOption(
        pipeline_options=html_pipeline_opts,
        backend_options=html_backend_opts,
    )

    # PDF format options
    accelerator_opts = AcceleratorOptions(num_threads=4, device=AcceleratorDevice.AUTO)

    pdf_pipeline_opts = ThreadedPdfPipelineOptions(
        ocr_batch_size=4,
        layout_batch_size=4,
        table_batch_size=4,
    )
    pdf_pipeline_opts.enable_remote_services = bool(picture_opts)
    pdf_pipeline_opts.accelerator_options = accelerator_opts
    pdf_pipeline_opts.do_ocr = config.docling_enable_ocr
    pdf_pipeline_opts.ocr_options = EasyOcrOptions()
    pdf_pipeline_opts.ocr_options.lang = ["en"]
    pdf_pipeline_opts.do_code_enrichment = True
    pdf_pipeline_opts.do_formula_enrichment = True
    pdf_pipeline_opts.generate_page_images = True
    pdf_pipeline_opts.do_picture_classification = config.docling_enable_picture_classification
    if builtin_description_active:
        pdf_pipeline_opts.do_picture_description = True
        pdf_pipeline_opts.picture_description_options = picture_opts
    else:
        pdf_pipeline_opts.do_picture_description = False
    pdf_pipeline_opts.generate_picture_images = True
    pdf_pipeline_opts.images_scale = 2
    pdf_pipeline_opts.do_table_structure = config.docling_enable_table_structure
    pdf_pipeline_opts.generate_table_images = True
    pdf_pipeline_opts.table_structure_options.do_cell_matching = True

    pdf_format = PdfFormatOption(pipeline_options=pdf_pipeline_opts)

    return DocumentConverter(
        format_options={
            InputFormat.HTML: html_format,
            InputFormat.PDF: pdf_format,
        }
    )


def _get_classification_label(pic: PictureItem) -> str | None:
    """Return the top classification label for a picture, or None if absent."""
    for annotation in pic.annotations:
        if isinstance(annotation, PictureClassificationData) and annotation.predicted_classes:
            label = annotation.predicted_classes[0].class_name
            logger.debug("Figure %s classification: %s", pic.self_ref, label)
            return label
    return None


def _call_vlm_api(image: Image.Image, prompt: str, config: ConversionConfig) -> str:
    """Call the VLM chat completions API with an image and prompt.

    Args:
        image: PIL image to describe.
        prompt: Task-specific prompt string.
        config: Conversion config with endpoint and auth settings.

    Returns:
        Response content string from the VLM.

    Raises:
        DoclingError: On HTTP or timeout error (retryable).
    """
    buf = BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    base_url = config.chat_completions_base_url.strip().rstrip("/")
    api_key = config.chat_completions_api_key.strip()

    payload = {
        "model": config.docling_vlm_model,
        "seed": 42,
        "reasoning_effort": "low",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{base_url}/chat/completions"

    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=config.docling_picture_timeout)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        raise DoclingError(f"VLM API call failed: {exc}") from exc


def _enrich_picture_descriptions(doc: DoclingDocument, config: ConversionConfig) -> None:
    """Post-conversion enrichment: classify each figure and call VLM with task-specific prompt.

    Only runs when both picture description and picture classification are enabled.
    Appends PictureDescriptionData to each figure's annotations, replacing Docling's
    built-in description pass.

    Args:
        doc: DoclingDocument after conversion (figures already classified).
        config: Conversion config.
    """
    if not config.is_picture_description_enabled():
        return
    if not config.docling_enable_picture_classification:
        return

    with trace_model_call(
        name="llm.chat.completions.docling_picture_description",
        span_type="CHAT_MODEL",
        attributes={
            "model": config.docling_vlm_model,
            "pipeline": "enrichment",
            "provider_endpoint": "/chat/completions",
        },
    ):
        for pic in doc.pictures:
            label = _get_classification_label(pic)
            if label is not None and label not in _LABEL_TO_PROMPT:
                logger.debug("Figure %s: unknown classifier label %r, using generic prompt", pic.self_ref, label)
            prompt = _LABEL_TO_PROMPT.get(label, _ALT_TEXT_PROMPT)

            image = pic.get_image(doc)
            if image is None:
                logger.debug("Figure %s: no image available, skipping description", pic.self_ref)
                continue

            result = _call_vlm_api(image, prompt, config)
            pic.annotations.append(PictureDescriptionData(text=result, provenance="aizk:vlm_enrichment"))


def _docling_to_markdown(doc: DoclingDocument) -> str:
    """Serialize DoclingDocument to Markdown with annotations."""
    from typing import Any

    class AnnotationPictureSerializer(MarkdownPictureSerializer):
        def serialize(
            self,
            *,
            item: PictureItem,
            doc_serializer: BaseDocSerializer,
            doc: DoclingDocument,
            separator: Optional[str] = None,
            **kwargs: Any,
        ) -> SerializationResult:
            text_parts: list[str] = []

            # Get base markdown representation
            parent_res = super().serialize(
                item=item,
                doc_serializer=doc_serializer,
                doc=doc,
                **kwargs,
            )
            text_parts.append(parent_res.text)

            # Prepend figure type label if classification annotation is present
            for annotation in item.annotations:
                if isinstance(annotation, PictureClassificationData) and annotation.predicted_classes:
                    label = annotation.predicted_classes[0].class_name
                    text_parts.append(f"<!-- Figure Type: {label} -->")
                    break

            # Append alt text as HTML comment
            for annotation in item.annotations:
                if isinstance(annotation, PictureDescriptionData):
                    text_parts.append(
                        f"""
<!-- Figure Description -->
{annotation.text}
<!-- End Figure Description -->
""".strip()
                    )

            text_res = (separator or "\n").join(text_parts)
            return create_ser_result(text=text_res, span_source=item)

    serializer = MarkdownDocSerializer(
        doc=doc,
        picture_serializer=AnnotationPictureSerializer(),
        table_serializer=HTMLTableSerializer(),
        params=MarkdownParams(
            enable_chart_tables=True,
            image_mode=ImageRefMode.PLACEHOLDER,
            image_placeholder="",
            include_annotations=False,
            mark_meta=True,
        ),
    )
    ser_result = serializer.serialize()
    markdown_text = ser_result.text

    if not markdown_text or not markdown_text.strip():
        raise DoclingEmptyOutputError()

    return markdown_text


def _extract_figures(doc: DoclingDocument, output_dir: Path) -> list[Path]:
    """Extract and save figures from DoclingDocument using native get_image API.

    Uses docling's built-in PictureItem.get_image() method to retrieve pre-rendered
    PIL Image objects directly, avoiding manual URI reconstruction.

    Args:
        doc: DoclingDocument with picture items.
        output_dir: Directory to save extracted figures.

    Returns:
        List of paths to saved figure files.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []

    for i, pic in enumerate(doc.pictures, 1):
        if not isinstance(pic, PictureItem):
            continue

        try:
            pil_image = pic.get_image(doc)
            if pil_image is None:
                logger.debug("Figure %s returned no image", pic.self_ref)
                continue

            output_path = output_dir / f"figure-{i:03d}.png"
            pil_image.save(output_path, format="PNG")
            logger.debug("Saved figure: %s", output_path)
            saved_paths.append(output_path)

        except Exception as e:
            logger.warning("Failed to extract figure %d: %s", i, e)
            continue

    return saved_paths


def convert_html(
    html_bytes: bytes,
    temp_dir: Path,
    config: ConversionConfig,
    source_url: Optional[str] = None,
) -> tuple[str, list[Path]]:
    """Convert HTML to Markdown using Docling.

    Args:
        html_bytes: HTML content as bytes.
        temp_dir: Temporary directory for extracted figures.
        config: Conversion configuration.
        source_url: Optional source URL for resolving relative links/images.

    Returns:
        Tuple of (markdown_text, list_of_figure_paths).

    Raises:
        DoclingError: If conversion fails.
        DoclingEmptyOutputError: If no Markdown is produced.
    """
    try:
        converter = _create_document_converter(config, source_url=source_url)
        source = DocumentStream(name="document.html", stream=BytesIO(html_bytes))
        if config.is_picture_description_enabled() and not config.docling_enable_picture_classification:
            # Fallback: built-in Docling description (classification disabled)
            with trace_model_call(
                name="llm.chat.completions.docling_picture_description",
                span_type="CHAT_MODEL",
                attributes={
                    "model": config.docling_vlm_model,
                    "pipeline": "html",
                    "provider_endpoint": "/chat/completions",
                },
            ):
                conv_result = converter.convert(source)
        else:
            conv_result = converter.convert(source)
        doc = conv_result.document

        _enrich_picture_descriptions(doc, config)
        markdown = _docling_to_markdown(doc)

    except DoclingEmptyOutputError:
        raise
    except Exception as e:
        logger.exception("HTML conversion failed")
        raise DoclingError(str(e)) from e
    else:
        figures = _extract_figures(doc, figure_dir(temp_dir))
        return markdown, figures


def convert_pdf(
    pdf_bytes: bytes,
    temp_dir: Path,
    config: ConversionConfig,
) -> tuple[str, list[Path]]:
    """Convert PDF to Markdown using Docling.

    Args:
        pdf_bytes: PDF content as bytes.
        temp_dir: Temporary directory for extracted figures.
        config: Conversion configuration.

    Returns:
        Tuple of (markdown_text, list_of_figure_paths).

    Raises:
        DoclingError: If conversion fails.
        DoclingEmptyOutputError: If no Markdown is produced.
    """
    try:
        converter = _create_document_converter(config)
        source = DocumentStream(name="document.pdf", stream=BytesIO(pdf_bytes))
        if config.is_picture_description_enabled() and not config.docling_enable_picture_classification:
            # Fallback: built-in Docling description (classification disabled)
            with trace_model_call(
                name="llm.chat.completions.docling_picture_description",
                span_type="CHAT_MODEL",
                attributes={
                    "model": config.docling_vlm_model,
                    "pipeline": "pdf",
                    "provider_endpoint": "/chat/completions",
                },
            ):
                conv_result = converter.convert(source)
        else:
            conv_result = converter.convert(source)
        doc = conv_result.document

        _enrich_picture_descriptions(doc, config)
        markdown = _docling_to_markdown(doc)

    except DoclingEmptyOutputError:
        raise
    except Exception as e:
        logger.exception("PDF conversion failed")
        raise DoclingError(str(e)) from e
    else:
        figures = _extract_figures(doc, figure_dir(temp_dir))
        return markdown, figures
