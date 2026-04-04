#!/usr/bin/env python3
"""Karakeep bookmark ingestion demo.

This notebook pulls bookmarks from the Karakeep API, runs assets and links through
Docling's PDF and HTML pipelines (including picture description VLM enrichment),
exports Markdown plus extracted images,..
Environment variables such as ``KARAKEEP_API_KEY``, ``KARAKEEP_BASE_URL``.
"""

# %%
import asyncio
import base64
from dataclasses import dataclass
import datetime
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path, PurePath
import re
import sys
from typing import Any, Iterable, Literal, Optional, cast
from urllib.parse import urlparse
from urllib.request import urlopen

from dotenv import load_dotenv
from pydantic import AnyUrl
from setproctitle import setproctitle
from tqdm.auto import tqdm
from typing_extensions import override

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.backend_options import HTMLBackendOptions, MarkdownBackendOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    ConvertPipelineOptions,
    EasyOcrOptions,
    PictureDescriptionApiOptions,
    ThreadedPdfPipelineOptions,
)
from docling.document_converter import DocumentConverter, HTMLFormatOption, MarkdownFormatOption, PdfFormatOption
from docling_core.transforms.chunker.hierarchical_chunker import TripletTableSerializer
from docling_core.transforms.serializer.base import (
    BaseDocSerializer,
    SerializationResult,
)
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
    PictureTabularChartData,
    TableItem,
)
from docling_core.types.io import DocumentStream

from aizk.datamodel.schema import ScrapeStatus, Source
from karakeep_client.karakeep import APIError, AuthenticationError, KarakeepClient, get_all_urls
from karakeep_client.models import (
    Bookmark,
    ContentTypeAsset,
    ContentTypeLink,
    ContentTypeText,
    PaginatedBookmarks,
)

# %%
# define python process name
setproctitle(Path(__file__).stem)

# Set up logging
logging.basicConfig(level=logging.INFO)

aizk_logger = logging.getLogger("aizk")
aizk_logger.setLevel(logging.DEBUG)

karakeep_logger = logging.getLogger("karakeep_client")
karakeep_logger.setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# %%
_ = load_dotenv()


# %%
def slugify(text: str) -> str:
    """Return a filesystem-friendly slug."""

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-").lower()
    return cleaned


def ensure_directory(path: Path) -> Path:
    """Create ``path`` if needed and return it."""

    path.mkdir(parents=True, exist_ok=True)
    return path


# %%
def resolve_bookmark_source_url(bookmark: Bookmark) -> AnyUrl:
    """Return the canonical source URL for a Karakeep bookmark."""

    content = bookmark.content
    if isinstance(content, ContentTypeLink):
        return AnyUrl(content.url)
    if isinstance(content, ContentTypeText) and content.source_url:
        return AnyUrl(content.source_url)
    if isinstance(content, ContentTypeAsset) and content.source_url:
        return AnyUrl(content.source_url)
    raise ValueError(f"Bookmark {bookmark.id} does not expose a source URL")


def resolve_bookmark_type(bookmark: Bookmark) -> str:
    """Return the bookmark's type.

    Prefers the top-level bookmark `type` field (when present), falling back to
    the embedded content `type`. Returns "unknown" when neither exists.
    """

    bookmark_type = getattr(bookmark, "type", None)
    if bookmark_type:
        return str(bookmark_type)

    content = getattr(bookmark, "content", None)
    content_type = getattr(content, "type", None)
    return str(content_type) if content_type else "unknown"


BookmarkContentKind = Literal["link", "text", "asset", "unknown"]


def resolve_bookmark_content_type(bookmark: Bookmark) -> BookmarkContentKind:
    """Return a normalized content type for a bookmark."""

    content = getattr(bookmark, "content", None)
    if isinstance(content, ContentTypeLink):
        return "link"
    if isinstance(content, ContentTypeText):
        return "text"
    if isinstance(content, ContentTypeAsset):
        return "asset"
    return "unknown"


def resolve_bookmark_content_text(bookmark: Bookmark) -> Optional[str]:
    """Return a best-effort textual representation of bookmark content.

    - Link: returns `html_content` when present, otherwise `None`.
    - Text: returns `text`.
    - Asset: returns extracted `content` when present (e.g., OCR/PDF text).

    Returns:
        The extracted text/HTML/URL, or `None` when no usable content exists.
    """

    content = getattr(bookmark, "content", None)
    if isinstance(content, ContentTypeLink):
        return content.html_content
    if isinstance(content, ContentTypeText):
        return content.text
    if isinstance(content, ContentTypeAsset):
        extracted = getattr(content, "content", None)
        return extracted
    return None


# %%
ALT_TEXT_INSTRUCTIONS = """
Provide a detailed description for this figure that captures the main subject, action, and any critical visual information including key components, relationships, and outcomes shown with the goal that someone who reads the description would be able to redraw the figure. Prioritize detail over brevity.

- Do not rely on phrases like "shown above" or "as seen"
- Do not say "image of," "picture of," "figure of," or "graphic of"
- Do not describe purely visual styling unless meaningful
- Do not guess emotions, identities, or intent unless clearly conveyed

Respond ONLY with the description, no other text.
""".strip()


def build_picture_description_options() -> PictureDescriptionApiOptions:
    """Configure picture descriptions to use the OpenRouter endpoint."""

    base_url = os.environ["_OPENROUTER_BASE_URL"].rstrip("/")
    api_key = os.environ["_OPENROUTER_API_KEY"]
    model_name = os.environ.get("DOCLING_VLM_MODEL", "openai/gpt-5.4-nano")
    timeout = float(os.environ.get("DOCLING_PICTURE_TIMEOUT", "180"))
    return PictureDescriptionApiOptions(
        url=AnyUrl(f"{base_url}/chat/completions"),
        params={
            "model": model_name,
            "seed": 42,
            "reasoning_effort": "low",
            # "max_completion_tokens": 2400,
        },
        headers={"Authorization": f"Bearer {api_key}"},
        prompt=ALT_TEXT_INSTRUCTIONS,
        timeout=timeout,
    )


def build_html_format_options(
    picture_description_options: PictureDescriptionApiOptions,
    source_uri: Optional[AnyUrl | PurePath] = None,
) -> HTMLFormatOption:
    """Return HTMLFormatOption for HTML conversions using picture descriptions."""

    pipeline_options = ConvertPipelineOptions()
    pipeline_options.enable_remote_services = True
    pipeline_options.do_picture_description = True
    pipeline_options.picture_description_options = picture_description_options

    html_backend_options = HTMLBackendOptions(
        kind="html",
        fetch_images=True,
        enable_remote_fetch=True,
        enable_local_fetch=True,
        source_uri=source_uri if source_uri else None,
    )

    return HTMLFormatOption(
        pipeline_options=pipeline_options,
        backend_options=html_backend_options,
    )


def build_md_format_options(
    picture_description_options: PictureDescriptionApiOptions,
    source_uri: Optional[AnyUrl | PurePath] = None,
) -> MarkdownFormatOption:
    """Return ConvertPipelineOptions for HTML conversions using picture descriptions."""

    pipeline_options = ConvertPipelineOptions()
    pipeline_options.enable_remote_services = True
    pipeline_options.do_picture_description = True
    pipeline_options.picture_description_options = picture_description_options

    md_backend_options = MarkdownBackendOptions(
        kind="md",
        fetch_images=True,
        enable_remote_fetch=True,
        enable_local_fetch=True,
        source_uri=source_uri if source_uri else None,
    )
    return MarkdownFormatOption(
        pipeline_options=pipeline_options,
        backend_options=md_backend_options,
    )


def build_pdf_format_options(
    picture_description_options: PictureDescriptionApiOptions,
) -> PdfFormatOption:
    """Return the PdfPipelineOptions used for docling conversions."""

    accelerator_options = AcceleratorOptions(
        num_threads=4,
        device=AcceleratorDevice.AUTO,
    )
    pipeline_options = ThreadedPdfPipelineOptions(
        ocr_batch_size=4,  # default 4
        layout_batch_size=4,  # default 4
        table_batch_size=4,  # currently not using GPU batching
    )
    pipeline_options.enable_remote_services = True
    pipeline_options.accelerator_options = accelerator_options
    pipeline_options.do_ocr = False
    pipeline_options.ocr_options.lang = ["en"]
    pipeline_options.do_code_enrichment = True
    pipeline_options.do_formula_enrichment = True
    pipeline_options.generate_page_images = True
    pipeline_options.do_picture_classification = False
    pipeline_options.do_picture_description = True
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = 2
    pipeline_options.do_table_structure = True
    pipeline_options.generate_table_images = True
    pipeline_options.table_structure_options.do_cell_matching = True
    pipeline_options.picture_description_options = picture_description_options

    return PdfFormatOption(pipeline_options=pipeline_options)


def create_docling_converter(
    picture_description_options: PictureDescriptionApiOptions,
    *,
    source_url: Optional[AnyUrl | PurePath] = None,
) -> DocumentConverter:
    """Instantiate a fresh DocumentConverter for both PDF and HTML formats."""

    html_options = build_html_format_options(picture_description_options, source_uri=source_url)
    pdf_options = build_pdf_format_options(picture_description_options)
    md_options = build_md_format_options(picture_description_options, source_uri=source_url)

    return DocumentConverter(
        format_options={
            InputFormat.HTML: html_options,
            InputFormat.PDF: pdf_options,
            InputFormat.MD: md_options,
        }
    )


# %%
class AnnotationPictureSerializer(MarkdownPictureSerializer):
    @override
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

        # reusing the existing result:
        parent_res = super().serialize(
            item=item,
            doc_serializer=doc_serializer,
            doc=doc,
            **kwargs,
        )
        text_parts.append(parent_res.text)

        # appending annotations:
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


def convert_docling_document_to_markdown(doc: DoclingDocument) -> str:
    """Serialize a docling document to markdown with annotations preserved."""

    serializer = MarkdownDocSerializer(
        doc=doc,
        picture_serializer=AnnotationPictureSerializer(),
        table_serializer=HTMLTableSerializer(),
        params=MarkdownParams(
            enable_chart_tables=True,
            image_mode=ImageRefMode.PLACEHOLDER,
            image_placeholder="",  # Do not inject image placeholder indicator
            include_annotations=False,  # or raw annotation text because customize in the AnnotationPictureSerializer
            mark_meta=True,
        ),
    )
    ser_result = serializer.serialize()
    return ser_result.text


def extract_and_save_figures(doc: DoclingDocument, output_dir: Path) -> int:
    """Extract and save figures from DoclingDocument using native get_image API.

    This function demonstrates the preferred approach for extracting figures from docling:

    1. Configure pipeline options with `generate_picture_images=True` (already done in
       build_pdf_format_options and build_html_format_options)
    2. Use PictureItem.get_image(doc) to retrieve pre-rendered PIL Image objects
    3. Save directly to PNG without manual URI reconstruction

    This leverages docling's native figure rendering at the configured resolution
    (images_scale=2 for 144 DPI) rather than trying to reconstruct images from URIs.

    Args:
        doc: The docling document containing picture items.
        output_dir: Directory where extracted figures are written.

    Returns:
        The number of figures successfully saved.
    """
    ensure_directory(output_dir)
    saved_count = 0

    for i, pic in enumerate(doc.pictures, 1):
        if not isinstance(pic, PictureItem):
            continue

        try:
            # Use docling's native get_image() API to retrieve PIL Image
            pil_image = pic.get_image(doc)
            if pil_image is None:
                logger.debug("Figure %s returned no image", pic.self_ref)
                continue

            figure_name = slugify(f"figure-{i:03d}.png")
            output_path = output_dir / figure_name

            pil_image.save(output_path, format="PNG")
            logger.debug("Saved figure: %s", output_path)
            saved_count += 1

        except Exception as e:
            logger.warning("Failed to extract figure %d: %s", i, e)
            continue

    return saved_count


# %%
# Initialize client
client = KarakeepClient(
    # disable_response_validation=True,
    verbose=True,
)

# %%
# Get bookmark by ID
### Asset
bookmark_id = "kbleumlsp93mtgx4r8dc6ext"  # Attention Is All You Need
# bookmark_id = "mt2vc0ziqqt0pz6ptaqbf7yn"  # LLMs for Scientific Idea Generation
### Link
# bookmark_id = "xt2omosp2erha7k4xd6mg9je"  # OpenAI ChatGPT Agent
# bookmark_id = "rpnt3mzc96g5uhovbv2runu4"  # Sycophancy and the Pepsi Challenge
# bookmark_id = "e8oks8mh930yfvcg2k0yzuvb"  # Treadmill 17 Jan 2025
# bookmark_id = "w1aiidzcsie8ug40nx21q9ko"  # Illustrated Guide to OAuth
# bookmark_id = "qks067chkb8t1kprtm7rqbxl"  # OpenAI Confessions

bookmark = await client.get_bookmark(bookmark_id=bookmark_id, include_content=True)

print(bookmark.model_dump_json(exclude={"tags"}, indent=2))

# %%
print("url: ", resolve_bookmark_source_url(bookmark))
print("bookmark type: ", resolve_bookmark_type(bookmark))
print("content type: ", resolve_bookmark_content_type(bookmark))
print("content text: ", resolve_bookmark_content_text(bookmark)[:500])
# bookmark content text may be None if not present; e.g., for asset bookmarks where the PDF is unable to be easily converted to text.

# %%
print(bookmark.content.model_dump_json(exclude={"tags"}, indent=2))


# %%
# TODO: figure out all possible options for bookmark type x content type?
# TODO: if arxiv.org asset, try to get the HTML directly instead of the PDF?

picture_description_options = build_picture_description_options()

bookmark_type = resolve_bookmark_type(bookmark)
content_type = resolve_bookmark_content_type(bookmark)

doc: Optional[DoclingDocument] = None

if bookmark_type == "asset" and content_type == "asset":
    content = bookmark.content
    asset_bytes = await client.get_asset(asset_id=content.asset_id)

    converter = create_docling_converter(picture_description_options)

    source = DocumentStream(name=f"{bookmark.id}.pdf", stream=BytesIO(asset_bytes))
    conv_result = converter.convert(source)
    doc = conv_result.document

if bookmark_type == "link" and content_type == "link":
    content = resolve_bookmark_content_text(bookmark)
    url = resolve_bookmark_source_url(bookmark)

    converter = create_docling_converter(picture_description_options, source_url=url)

    source = DocumentStream(name=f"{bookmark.id}.html", stream=BytesIO(content.encode("utf-8")))
    conv_result = converter.convert(source)
    doc = conv_result.document

if doc is None:
    raise ValueError(f"Unsupported bookmark type {bookmark_type!r} with content {content_type!r}")

image_dir_name_base = getattr(bookmark, "title", "") or bookmark.id
figures_output_dir = ensure_directory(Path("../data/extracted_images") / slugify(bookmark.id))
figure_count = extract_and_save_figures(doc, figures_output_dir)
logger.info("Extracted %d figures to %s", figure_count, figures_output_dir)

# %%
md = convert_docling_document_to_markdown(doc)
print(md)

# %%
