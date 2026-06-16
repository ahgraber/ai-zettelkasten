# %% [markdown]
# # Docling Conversion — Hands-On Tour
#
# A hands-on walkthrough of the production Docling conversion path. For a handful
# of real KaraKeep bookmarks it **selects the processing pipeline** (arXiv/GitHub/
# PDF-asset/HTML), **fetches the source bytes**, **converts to Markdown** with the
# same `aizk.conversion.processing.converter` calls the worker uses, applies the
# worker's whitespace normalization, and writes an `output.md` you can open and
# eyeball — so you can see exactly what the worker would persist.
#
# **Run model:** open in VS Code Interactive or Jupyter and execute cells
# top-to-bottom with **Shift+Enter**. State lives in module-scope variables that
# later cells reuse, so run the cells in order. The final driver cell uses
# top-level `await` (valid because the kernel owns the event loop).
#
# **Real infrastructure:** this tour does REAL work — it calls a real KaraKeep
# instance and runs real Docling conversion (and any configured VLM picture
# description). Set the KaraKeep + converter env vars (e.g. via `.env`/direnv)
# before running. There is no `__main__` guard: the cells run as you step through
# them, the way you would the `ai-vfs` tour.

# %%
from __future__ import annotations

import logging
import os
from pathlib import Path
import socket
from typing import Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
import nest_asyncio
from setproctitle import setproctitle

from aizk.conversion.processing.converter import convert_html, convert_pdf
from aizk.conversion.processing.fetcher import fetch_arxiv, fetch_github_readme, fetch_karakeep_asset
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
from aizk.conversion.utilities.config import DoclingConverterConfig
from aizk.conversion.utilities.whitespace import normalize_whitespace
from karakeep_client.models import Bookmark

# %% [markdown]
# ## 1. Setup
#
# Boilerplate you rarely step through: `nest_asyncio` lets the synchronous
# `fetch_karakeep_bookmark` helper drive its async client on the kernel's running
# loop; `setproctitle` gives the process a descriptive name; logging is turned up
# so you can watch the conversion stages; `load_dotenv` brings in the KaraKeep +
# converter credentials; and `output_dir` is where each bookmark's `output.md`
# lands.

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

output_dir = Path("data/validate_docling_worker")
output_dir.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# ## Probe KaraKeep before converting (no work yet)
#
# This tour calls a real KaraKeep instance and runs real Docling conversion, so the
# first thing to confirm is that KaraKeep is configured and reachable. Set
# `AIZK_FETCHER__KARAKEEP__API_KEY` and `AIZK_FETCHER__KARAKEEP__BASE_URL` (e.g. via
# `.env`/direnv). If either is missing or the host is unreachable, this prints what
# to fix with no traceback; the fetch/convert cells below assume it succeeded.

# %%
_kk_api_key = os.environ.get("AIZK_FETCHER__KARAKEEP__API_KEY", "").strip()
_kk_base_url = os.environ.get("AIZK_FETCHER__KARAKEEP__BASE_URL", "").strip()
if not _kk_api_key or not _kk_base_url:
    print("KaraKeep is not configured — the cells below need it. Set these, then re-run:")
    print("  AIZK_FETCHER__KARAKEEP__API_KEY=<token>")
    print("  AIZK_FETCHER__KARAKEEP__BASE_URL=https://karakeep.example.com")
else:
    _parsed = urlparse(_kk_base_url)
    _host = _parsed.hostname or ""
    _port = _parsed.port or (443 if _parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((_host, _port), timeout=3.0):
            print(f"KaraKeep reachable at {_host}:{_port} — later cells will fetch and convert.")
    except OSError as exc:
        print(f"KaraKeep configured but {_host}:{_port} is unreachable: {exc}")
        print(f"Check AIZK_FETCHER__KARAKEEP__BASE_URL ({_kk_base_url!r}) and that the instance is up.")

# %% [markdown]
# ## 2. Fetch & prepare source content
#
# `fetch_source_content` mirrors the worker's source-selection logic: arXiv
# bookmarks resolve to a downloaded PDF, GitHub bookmarks to rendered README HTML,
# PDF assets to their stored bytes, and everything else falls back to the
# bookmark's HTML (or its text content wrapped in HTML). It returns the chosen
# pipeline (`"pdf"` / `"html"`) and the raw bytes to convert.

# %%
# process job: fetch & prepare source content
ProcessingPipeline = Literal["html", "pdf"]


async def fetch_source_content(bookmark: Bookmark, source_type: str) -> tuple[ProcessingPipeline, bytes]:
    """Fetch source content bytes from KaraKeep bookmark."""

    config = DoclingConverterConfig()
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


# %% [markdown]
# ## 3. Convert to Markdown
#
# `convert_to_markdown` runs the production converter (`convert_pdf` /
# `convert_html`) into a per-bookmark workspace, then applies the **same
# whitespace normalization the worker applies before writing `output.md`** (see
# `aizk.conversion.processing.subproc`) — so the file on disk matches what the
# worker would persist. Set
# `AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED=false` to disable
# classification-based prompt routing.


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
    # Set AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED=false to disable classification-based prompt routing.
    config = DoclingConverterConfig()
    workspace = output_dir / bookmark_id
    workspace.mkdir(parents=True, exist_ok=True)

    if pipeline == "pdf":
        markdown_text, figure_paths, document_title = convert_pdf(content_bytes, workspace, config)
    else:
        markdown_text, figure_paths, document_title = convert_html(
            content_bytes, workspace, config, source_url=source_url
        )

    # Mirror the production worker: normalize whitespace before writing output.md
    # (see aizk.conversion.processing.subproc).
    markdown_text = normalize_whitespace(markdown_text)
    (workspace / "output.md").write_text(markdown_text)
    print(f"  title={document_title!r}  figures={len(figure_paths)}  -> {workspace / 'output.md'}")


# %% [markdown]
# ## 4. Convert a sample of bookmarks
#
# The driver: for each bookmark id, fetch the bookmark, run the same job-submission
# checks the API does (`validate_bookmark_content`, source/content-type detection),
# then fetch and convert. The four samples below span the pipelines — an arXiv PDF,
# a GitHub README, an HTML page with images, and a SingleFile capture. Swap in your
# own bookmark ids and re-run to convert different sources.

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

    pipeline, content_bytes = await fetch_source_content(bookmark, source_type)
    print(f"Selected processing pipeline: {pipeline}")

    convert_to_markdown(pipeline, content_bytes, output_dir, bookmark.id, source_url=source_url)

print("Docling conversion tour finished.")

# %%
