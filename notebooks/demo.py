# %%
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import typing as t

from IPython.core.getipython import get_ipython  # NOQA: E402
from IPython.core.interactiveshell import InteractiveShell  # NOQA: E402
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.core.database import (
    add_urls_to_backlog,
    get_db_engine,
    get_pending_sources,
    initialize_database,
    update_scraped_sources,
)
from aizk.extractors.base import ExtractionError, Extractor, ExtractorSettings
from aizk.extractors.chrome import (
    ChromeExtractor,
    ChromeHTMLExtractor,
    ChromeSettings,
)
from aizk.extractors.postlight_parser import PostlightExtractor, PostlightSettings

# from aizk.extractors.singlefile import SinglefileExtractor, SinglefileSettings

# %%
ipython: InteractiveShell | None = get_ipython()
if ipython is not None:
    ipython.run_line_magic("load_ext", "autoreload")
    ipython.run_line_magic("autoreload", "2")

# %%
logger = logging.getLogger(__file__)

# %%
repo = subprocess.check_output(  # NOQA: S603
    ["git", "rev-parse", "--show-toplevel"],  # NOQA: S607
    cwd=Path(__file__).parent,
    encoding="utf-8",
).strip()
repo = Path(repo).expanduser().resolve()

datadir = repo / "data"
datadir.mkdir(exist_ok=True)

# SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{datadir}/test.db"

# %%
# clean prior experiment
shutil.rmtree(datadir / "scrape", ignore_errors=True)
Path(SQLALCHEMY_DATABASE_URL.removeprefix("sqlite:///")).unlink(missing_ok=True)

# %%
engine = get_db_engine(
    SQLALCHEMY_DATABASE_URL,
    echo=True,  # for dev
)

# %%
initialize_database(engine)

# %%
urls = [
    "https://sqlmodel.tiangolo.com/tutorial/insert/#create-a-session",
    "https://reinforcedknowledge.com/transformers-attention-is-all-you-need/",
    # Attn is all you need
    "https://arxiv.org/abs/1706.03762",
    "https://arxiv.org/html/1706.03762v7",
    "https://arxiv.org/pdf/1706.03762",
    # hard to scrape (scrolling background effects, javascript animations/charts)
    "https://www.bloomberg.com/graphics/2023-generative-ai-bias/",
    "https://www.washingtonpost.com/technology/interactive/2023/ai-generated-images-bias-racism-sexism-stereotypes/",
    "https://www.washingtonpost.com/technology/interactive/2024/ai-bias-beautiful-women-ugly-images/",
]
add_urls_to_backlog(engine, urls)


# %%
pending = get_pending_sources(engine)

source = pending[0]

# %%

# %%
# from aizk.utilities.path_helpers import add_node_bin_to_PATH, find_binary_abspath

# find_binary_abspath("postlight-parser", add_node_bin_to_PATH())
# find_binary_abspath("single-file", add_node_bin_to_PATH())

# %%
chrome_dir = Path("./data/chrome")
chrome_dir.mkdir(exist_ok=True)

# %%
chrome_extractor = ChromeExtractor(out_dir=chrome_dir)

# %%
chrome_html_extractor = ChromeHTMLExtractor()  # (out_dir=chrome_dir)

# %%
extract = chrome_html_extractor.run(pending[1].url)

# %%
from subprocess import run  # NOQA: E402

cmd = [
    str(chrome_html_extractor.binary),
    *chrome_html_extractor.config.chrome_args(),
    "--dump-dom",
    pending[1].url,
]

result = run(  # NOQA: S603
    cmd,
    capture_output=True,
    timeout=chrome_html_extractor.config.CHROME_TIMEOUT,
)

# %%
postlight_dir = Path("./data/postlight-parser")
postlight_dir.mkdir(exist_ok=True)
postlight_extractor = PostlightExtractor(out_dir=postlight_dir)

# %%
postlight_extractor.exec(source)

# %%
extract = postlight_extractor.run(pending[4].url)

# %%
updates = []
for source in pending:
    processed_source = postlight_extractor.exec(source)
    updates.append(processed_source)

update_scraped_sources(engine, updates)

# %%
# updates = [
#     Source(
#         **links[0].model_dump(),
#         scraped_at=datetime.datetime.now(),
#         scrape_status=ScrapeStatus("COMPLETE"),
#         content_hash="1234qwer",
#         error_message=None,
#         file="./test",
#     ),
#     Source(
#         **links[1].model_dump(),
#         scraped_at=datetime.datetime.today(),
#         scrape_status=ScrapeStatus("ERROR"),
#         content_hash=None,
#         error_message="ERROR MESSAGE",
#         file=None,
#     ),
# ]
# update_scraped_sources(engine, updates)


# %%
from docling.document_converter import DocumentConverter  # NOQA: E402

# %%
# source = "https://arxiv.org/pdf/2408.09869"  # PDF path or URL
# source = "https://arxiv.org/html/2408.09869v3"
source = "https://reinforcedknowledge.com/transformers-attention-is-all-you-need/"
converter = DocumentConverter()
result = converter.convert(source)

print(result.document.export_to_markdown())  # output: "### Docling Technical Report[...]"
