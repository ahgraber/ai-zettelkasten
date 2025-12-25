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

# %%
# Add the src directory to the path so we can import treadmill
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aizk.utilities.arxiv import AsyncArxivClient, get_arxiv_paper_metadata
from aizk.utilities.url_utils import arxiv_abs_url, get_arxiv_id, is_arxiv_url, standardize_arxiv, to_arxiv_export_url

# %%
# define python process name
setproctitle(Path(__file__).stem)

# Set up logging
logging.basicConfig(level=logging.INFO)

treadmill_logger = logging.getLogger("treadmill")
treadmill_logger.setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


# %%
# Attn is all you need, Deepseek R-1
ids = ["1706.03762", "2501.12948"]

# %%
# Get arxiv metadata
client = AsyncArxivClient()
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
    print(f"Scrape PDF URL: {to_arxiv_export_url(metadata['pdf_url'])}")
    print("-" * 80)

# %%
