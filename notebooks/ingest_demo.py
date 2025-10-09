#!/usr/bin/env python3
"""Demo script for KarakeepClient usage.

This script demonstrates how to use the KarakeepClient to interact with the Karakeep API.
Make sure to set KARAKEEP_API_KEY and KARAKEEP_BASEURL environment variables.
"""

# %%
import asyncio
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Optional

from dotenv import load_dotenv
from tqdm.auto import tqdm
from typing_extensions import override
import zstandard as zstd

import pandas as pd

import requests

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    EasyOcrOptions,
    PdfPipelineOptions,
    PictureDescriptionApiOptions,
    VlmPipelineOptions,
)
from docling.datamodel.pipeline_options_vlm_model import ApiVlmOptions, ResponseFormat
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline
from docling_core.transforms.chunker.hierarchical_chunker import TripletTableSerializer
from docling_core.transforms.serializer.base import (
    BaseDocSerializer,
    SerializationResult,
)
from docling_core.transforms.serializer.common import create_ser_result
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
    TableItem,
)
from docling_core.types.doc.page import SegmentedPage
from docling_core.types.io import DocumentStream

from karakeep_client.karakeep import APIError, AuthenticationError, KarakeepClient, get_all_urls
from karakeep_client.models import PaginatedBookmarks

# %%
# Add the src directory to the path so we can import treadmill
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# %%
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
# Initialize client
client = KarakeepClient(
    # disable_response_validation=True,
    verbose=True,
)

# %%
# Get bookmark by ID
# bookmark_id = "y9drx8oxif2uljuzp1ujctv1"  # Attention Is All You Need
# bookmark_id = "xt2omosp2erha7k4xd6mg9je"  # OpenAI ChatGPT Agent
# bookmark_id = "rpnt3mzc96g5uhovbv2runu4"  # Sycophancy and the Pepsi Challenge
# bookmark_id = "e8oks8mh930yfvcg2k0yzuvb"  # Treadmill 17 Jan 2025
bookmark_id = "w1aiidzcsie8ug40nx21q9ko"  # Illustrated Guide to OAuth
bookmark = await client.get_bookmark(bookmark_id=bookmark_id, include_content=True)

bookmark.model_dump(exclude={"tags"})

# %%
# Get all bookmarks
logger.info("Fetching all bookmarks...")
next_cursor = None

bookmarks = []
with tqdm(desc="Fetching bookmarks", unit="bookmarks") as pbar:
    while True:
        try:
            bookmarks_response = await client.get_bookmarks_paged(cursor=next_cursor, limit=100)

            if not isinstance(bookmarks_response, PaginatedBookmarks):
                break
            bookmarks.extend(bookmarks_response.bookmarks)
            pbar.update(len(bookmarks_response.bookmarks))

            if not bookmarks_response.next_cursor:
                break
            next_cursor = bookmarks_response.next_cursor

        except Exception as e:
            logger.warning("Error fetching page: %s", e)
            break


# %%
output_file = Path("./data/bookmarks.json.zst")
output_file.parent.mkdir(parents=True, exist_ok=True)

with output_file.open("wb") as f:
    json_data = json.dumps([b.model_dump() for b in bookmarks], ensure_ascii=False).encode("utf-8")
    f.write(zstd.ZstdCompressor(level=3).compress(json_data))

logger.info("Saved %d bookmarks to %s", len(bookmarks), output_file)

# %%
bookmark_types = set()
bookmark_content_types = set()
for bookmark in bookmarks:
    try:
        bookmark_types.add(bookmark.type)
    except AttributeError:
        pass

    try:
        bookmark_content_types.add(bookmark.content.type)
    except AttributeError:
        pass

    if not hasattr(bookmark, "type") and not hasattr(bookmark.content, "type"):
        logger.warning("Bookmark without type: %s", bookmark)


# %%
# extracted PDF content
print(bookmark.content.content)

# %%
pdfs = []
images = []
text = []
for bookmark in bookmarks:
    try:
        if bookmark.content.type == "asset":
            # content = bookmark.content.content
            asset_id = bookmark.content.asset_id
            if bookmark.content.asset_type == "pdf":
                pdfs.append((bookmark.id, asset_id))
            elif bookmark.content.asset_type == "image":
                images.append((bookmark.id, asset_id))

            # try:
            #     asset = await client.get_asset(
            #         asset_id=bookmark.content.asset_id,
            #     )
            # except (APIError, AuthenticationError) as e:
            #     logger.exception(f"Error fetching asset: {e}")
            #     asset = None

        elif bookmark.content.type == "link":
            content = bookmark.content.html_content
            text.append((bookmark.id, content))
        elif bookmark.content.type == "text":
            content = bookmark.content.text
            text.append((bookmark.id, content))
        else:
            logger.warning("Unknown bookmark: %s", bookmark.id)
        # print(content)
    except Exception:
        logger.exception("Error processing bookmark %s", bookmark.id)
        print(bookmark.model_dump())
        break

    if content is None:
        logger.warning("Content is None for bookmark %s", bookmark.id)
    print(bookmark.model_dump())

# %%
asset = await client.get_asset(
    asset_id=bookmark.content.asset_id,
)

# `get_asset` returns the raw content of the asset.
# with open("./data/Attention is all you need.pdf", "wb") as f:
#     f.write(asset


# %%
# [Batch conversion - Docling](https://docling-project.github.io/docling/examples/batch_convert/)
# [Table export - Docling](https://docling-project.github.io/docling/examples/export_tables/)
# [VLM pipeline with remote model - Docling](https://docling-project.github.io/docling/examples/vlm_pipeline_api_model/)

# %%
# Explicitly set the accelerator
# accelerator_options = AcceleratorOptions(
#     num_threads=8, device=AcceleratorDevice.AUTO
# )
# accelerator_options = AcceleratorOptions(
#     num_threads=8, device=AcceleratorDevice.CPU
# )
accelerator_options = AcceleratorOptions(num_threads=8, device=AcceleratorDevice.MPS)
# accelerator_options = AcceleratorOptions(
#     num_threads=8, device=AcceleratorDevice.CUDA
# )

picture_description_options = PictureDescriptionApiOptions(
    # url=os.environ["_LOCAL_BASE_URL"],
    url="http://10.1.0.203:1234/v1/chat/completions",
    params=dict(  # NOQA: C408
        # model="gemma3:4b-it-qat",
        model="google/gemma-3-27b",
        seed=42,
        max_completion_tokens=1000,
    ),
    prompt="Describe the image in 3-5 sentences. Be accurate but concise. Respond ONLY with the description, no other text.",
    timeout=180,
)

pipeline_options = PdfPipelineOptions()
pipeline_options.enable_remote_services = True

pipeline_options.accelerator_options = accelerator_options
pipeline_options.do_ocr = False
pipeline_options.ocr_options.lang = ["en"]

pipeline_options.do_code_enrichment = True
pipeline_options.do_formula_enrichment = True

pipeline_options.do_picture_classification = False
pipeline_options.do_picture_description = True
pipeline_options.generate_picture_images = True
pipeline_options.images_scale = 2

pipeline_options.do_table_structure = True
pipeline_options.generate_table_images = True
pipeline_options.table_structure_options.do_cell_matching = True

pipeline_options.picture_description_options = picture_description_options

converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)})

# %%
# convert from binary stream
buf = BytesIO(asset)
source = DocumentStream(name="attention is all you need.pdf", stream=buf)
# source = "https://arxiv.org/pdf/2501.17887"

conv_res = converter.convert(source)
doc = conv_res.document

# # %%
# # naive serialization to markdown
# print(doc.export_to_markdown())

# # %%
# for table_ix, table in enumerate(doc.tables):
#     print(f"Table {table_ix}:")
#     print(table.export_to_html(doc=doc))

# # %%
# pictures = [element for element, _level in conv_res.document.iterate_items() if isinstance(element, PictureItem)]

# for picture in pictures:
#     print(f"Picture {picture.self_ref}")
#     print(f"Caption: {picture.caption_text(doc=doc)}")
#     for annotation in picture.annotations:
#         if isinstance(annotation, PictureDescriptionData):
#             print(f"Description: {annotation.text}")
#         else:
#             print(f"Annotation: {annotation}")

# %%
# [Serialization - Docling](https://docling-project.github.io/docling/examples/serialization/#configuring-a-serializer)


# %%
serializer = MarkdownDocSerializer(
    doc=doc,
    # picture_serializer=AnnotationPictureSerializer(),
    table_serializer=HTMLTableSerializer(),
    params=MarkdownParams(
        enable_chart_tables=True,
        image_mode=ImageRefMode.PLACEHOLDER,  # ImageRefMode.REFERENCED,
        image_placeholder="",
        mark_annotations=True,
    ),
)
ser_result = serializer.serialize()
ser_text = ser_result.text

# # %%
# # picture serialization
# start_cue = "Most competitive neural sequence transduction models"
# stop_cue = "left and right halves of Figure 1, respectively."

# print(ser_text[ser_text.find(start_cue) : ser_text.find(stop_cue)])

# # %%
# # table serialization
# start_cue = "To evaluate the importance of different components of the Transformer"
# stop_cue = "In Table 3 rows (B)"
# print(ser_text[ser_text.find(start_cue) : ser_text.find(stop_cue)])


# %%
output_dir = Path("./data/attention_is_all_you_need")
output_dir.mkdir(parents=True, exist_ok=True)
doc_filename = "attention_is_all_you_need"
# doc_filename = conv_res.input.file.stem

with (output_dir / f"{doc_filename}.md").open("w") as fp:
    fp.write(ser_text)

# %%
# Save images of figures and tables
picture_counter = 0
table_counter = 0
for element, _level in conv_res.document.iterate_items():
    if isinstance(element, PictureItem):
        fig = element.get_image(conv_res.document)
        if fig is None:
            logger.warning(f"Picture {element.self_ref} has no image, skipping.")
            continue

        picture_counter += 1
        element_image_filename = output_dir / f"{doc_filename}-figure-{picture_counter}.png"
        with element_image_filename.open("wb") as fp:
            fig.save(fp, "PNG", optimize=True, compress_level=6)

    if isinstance(element, TableItem):
        fig = element.get_image(conv_res.document)
        if fig is None:
            logger.warning(f"Table {element.self_ref} has no image, skipping.")
            continue

        table_counter += 1
        element_image_filename = output_dir / f"{doc_filename}-table-{table_counter}.png"
        with element_image_filename.open("wb") as fp:
            fig.save(fp, "PNG", optimize=True, compress_level=6)


# %%
