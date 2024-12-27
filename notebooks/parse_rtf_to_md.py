# %%
import asyncio
import logging
from pathlib import Path
import re
import urllib.parse as urlparse

import aiohttp
from bs4 import BeautifulSoup
import chardet
import pypandoc

import pandas as pd

from aizk.utilities.parse import URL_REGEX, detect_encoding, extract_md_url, extract_url, find_all_urls, validate_url

logging.basicConfig()
logger = logging.getLogger(__name__)

# %%
data_dir = Path(__file__).parents[1] / "data"
treadmill = data_dir / "treadmill"


# %%
def decode_textfile(file: Path) -> str:
    """Decode file with unknown encoding."""
    with file.open("rb") as f:
        blob = f.read()
        enc = detect_encoding(blob)

        return blob.decode(enc)


# %%
# read in rtf files and convert to markdown
for file in (treadmill / "raw").rglob("*.rtf"):
    md = pypandoc.convert_text(
        decode_textfile(file),  # pandoc expects utf-8 encoded files
        "gfm",  # github format markdown
        format="rtf",  # text format, since convert_text can't infer from filename
    )

    outfile = file.parent.stem if ".rtfd" in str(file) else file.stem
    with (treadmill / "md" / (outfile + ".md")).open("w") as out:
        out.write(md)
