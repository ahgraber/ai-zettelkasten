#!/usr/bin/env python3
"""Demo script for ArxivClient usage."""

# %%
import asyncio
import json
import logging
import os
from pathlib import Path
import sys

from setproctitle import setproctitle

from aizk.conversion.utilities.arxiv_utils import (
    ArxivClient,
    arxiv_pdf_url,
    get_arxiv_id,
)

# %%
# define python process name
setproctitle(Path(__file__).stem)

# Set up logging
logging.basicConfig(level=logging.INFO)

aizk_logger = logging.getLogger("aizk")
aizk_logger.setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


# %%
# Attn is all you need, Deepseek R-1
ids = ["1706.03762", "2501.12948"]

# %%
# Get arxiv metadata
async with ArxivClient() as client:
    paper_metadata = await client.get_paper_metadata({"1706.03762", "2501.12948"})

# %%
print(json.dumps(paper_metadata, indent=2))

# %%
for metadata in paper_metadata:
    print(f"Arxiv ID: {get_arxiv_id(metadata['id'])}")
    print(f"Title: {metadata['title']}")
    # print(f"Authors: {', '.join(metadata['authors'])}")
    # print(f"Published: {metadata['published']}")
    print(f"Summary: {metadata['summary']}")
    print(f"Original PDF URL: {metadata['pdf_url']}")
    print(f"Scrape PDF URL: {arxiv_pdf_url(get_arxiv_id(metadata['id']), use_export_url=True)}")
    print("-" * 80)

# %%
