#!/usr/bin/env python3
"""Convert a single KaraKeep bookmark using its PDF asset only."""

# %%
from __future__ import annotations

import logging
from pathlib import Path
import tempfile

from dotenv import load_dotenv
import nest_asyncio

from aizk.conversion.utilities.bookmark_utils import (
    fetch_karakeep_bookmark,
    get_bookmark_asset_id,
    is_pdf_asset,
)
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.paths import OUTPUT_MARKDOWN_FILENAME, figure_dir, markdown_path
from aizk.conversion.workers.converter import convert_pdf
from aizk.conversion.workers.fetcher import fetch_karakeep_asset
from aizk.utilities.async_utils import run_async

nest_asyncio.apply()

# %%
_ = load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# %%
KARAKEEP_ID = "diel9q68l7ku5g1e569u77lo"


# %%
bookmark = fetch_karakeep_bookmark(KARAKEEP_ID)
if not bookmark:
    raise RuntimeError(f"Bookmark {KARAKEEP_ID} not found in KaraKeep")

if not is_pdf_asset(bookmark):
    raise RuntimeError(f"Bookmark {KARAKEEP_ID} is not a PDF asset; refusing to use extracted text")

asset_id = get_bookmark_asset_id(bookmark)
if not asset_id:
    raise RuntimeError(f"Bookmark {KARAKEEP_ID} has no asset id")


# %%
config = ConversionConfig()
asset_bytes = run_async(fetch_karakeep_asset, asset_id)
