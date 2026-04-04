#!/usr/bin/env python3
"""Docling worker demo."""

# %%
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
import nest_asyncio
from setproctitle import setproctitle

from aizk.conversion.utilities.bookmark_utils import (
    BookmarkContentError,
    detect_content_type,
    detect_source_type,
    fetch_karakeep_bookmark,
    get_bookmark_asset_id,
    get_bookmark_html_content,
    get_bookmark_source_url,
    get_bookmark_text_content,
    is_pdf_asset,
    validate_bookmark_content,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers.converter import convert_html, convert_pdf
from aizk.conversion.workers.fetcher import fetch_arxiv, fetch_github_readme, fetch_karakeep_asset
from karakeep_client.models import Bookmark

# %%
nest_asyncio.apply()

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

output_dir = Path("data/docling_demo_worker")
output_dir.mkdir(parents=True, exist_ok=True)

# %%
# process job: fetch & prepare source content
ProcessingPipeline = Literal["html", "pdf"]


async def fetch_source_content(bookmark: Bookmark) -> tuple[ProcessingPipeline, bytes]:
    """Fetch source content bytes from KaraKeep bookmark."""

    config = ConversionConfig()
    if source_type == "arxiv":
        # fetch_arxiv internal logic:
        #   1. If source URL is arxiv.org/abs (abstract page) → download PDF from arXiv
        #   2. If bookmark is a PDF asset → use it (or fetch from KaraKeep)
        #   3. If link bookmark with html content → download PDF from arXiv
        content_bytes = await fetch_arxiv(bookmark, config)
        return "pdf", content_bytes

    if source_type == "github":
        content_bytes = await fetch_github_readme(bookmark, config)
        return "html", content_bytes

    if is_pdf_asset(bookmark):
        asset_id = get_bookmark_asset_id(bookmark)
        if asset_id:
            content_bytes = await fetch_karakeep_asset(asset_id)
            return "pdf", content_bytes

    # Fallback to HTML content
    html_content = get_bookmark_html_content(bookmark)
    if html_content:
        return "html", html_content.encode("utf-8")

    text_content = get_bookmark_text_content(bookmark)
    if text_content:
        html = f"<html><body><pre>{text_content}</pre></body></html>"
        return "html", html.encode("utf-8")

    raise BookmarkContentError(f"Bookmark {bookmark.id} has no fetchable content")


# %%
# process job: convert to Markdown
def convert_to_markdown(
    pipeline: ProcessingPipeline,
    content_bytes: bytes,
    output_dir: Path,
    bookmark_id: str,
    source_url: str | None = None,
) -> None:
    """Convert source content bytes to Markdown and save to output directory."""
    # ConversionConfig reads DOCLING_ENABLE_PICTURE_CLASSIFICATION from the environment
    # (default: True). Set it to "false" to disable classification-based prompt routing
    # and fall back to a single generic alt-text prompt for all figures.
    config = ConversionConfig()
    workspace = output_dir / bookmark_id
    workspace.mkdir(parents=True, exist_ok=True)

    if pipeline == "pdf":
        markdown_text, figure_paths = convert_pdf(content_bytes, workspace, config)
        (workspace / "output.md").write_text(markdown_text)
    else:
        markdown_text, figure_paths = convert_html(content_bytes, workspace, config, source_url=source_url)
        (workspace / "output.md").write_text(markdown_text)


# %%
bookmarks = [
    "kbleumlsp93mtgx4r8dc6ext",  # Attention Is All You Need | Arxiv
    "hojcn565u2m9smwtoehhjz3q",  # tinysearch | Github
    "w1aiidzcsie8ug40nx21q9ko",  # Illustrated Guide to OAuth | HTML with images
    "tufj0yp05tiqu485z4ocxs0u",  # OpenAI Sensitive Convos | Singlefile
]
for bookmark_id in bookmarks:
    bookmark = fetch_karakeep_bookmark(bookmark_id)  # needs nest_asyncio

    # job submission procedure
    validate_bookmark_content(bookmark)
    source_url = get_bookmark_source_url(bookmark)
    source_type = detect_source_type(source_url)
    content_type = detect_content_type(bookmark)

    print(
        f"""
    Bookmark ID: {bookmark.id}
    Bookmark Title: {bookmark.title}
    Source URL: {source_url}
    Source Type: {source_type}
    Content Type: {content_type}
    """.strip()
    )

    pipeline, content_bytes = await fetch_source_content(bookmark)
    print(f"Selected processing pipeline: {pipeline}")

    convert_to_markdown(pipeline, content_bytes, output_dir, bookmark.id, source_url=source_url)

print("Docling demo worker finished.")

# %%
