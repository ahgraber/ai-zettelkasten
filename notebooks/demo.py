# %%
import json
import logging
import os
from pathlib import Path
import shutil
import typing as t
from urllib.parse import quote, unquote, urlparse

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
from aizk.datamodel.schema import ScrapeStatus, Source
from aizk.extractors import (
    STATICFILE_EXTENSIONS,
    ArxivExtractor,
    ArxivSettings,
    ChromeExtractor,
    ChromeSettings,
    ExtractionError,
    # Extractor,
    ExtractorSettings,
    GitHubExtractor,
    PlaywrightExtractor,
    PlaywrightSettings,
    PostlightExtractor,
    PostlightSettings,
    SingleFileExtractor,
    SingleFileSettings,
    StaticFileExtractor,
)
from aizk.extractors.chrome import detect_playwright_chromium
from aizk.utilities import AsyncTimeWindowRateLimiter, TimeWindowRateLimiter, basic_log_config, get_repo_path
from aizk.utilities.async_helpers import synchronize

# %%
ipython: InteractiveShell | None = get_ipython()
if ipython is not None:
    ipython.run_line_magic("load_ext", "autoreload")
    ipython.run_line_magic("autoreload", "2")

# %%
basic_log_config()

logger = logging.getLogger(__file__)
logger.setLevel(logging.DEBUG)

# %%
repo = get_repo_path(__file__)

data_dir = repo / "app" / "archive"
data_dir.mkdir(exist_ok=True, parents=True)
# savedir = datadir / "scrape"

# %%
# SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{data_dir}/test.db"

# %%
# clean prior experiment
shutil.rmtree(data_dir / "scrape", ignore_errors=True)
Path(SQLALCHEMY_DATABASE_URL.removeprefix("sqlite:///")).unlink(missing_ok=True)

# %%
arxiv_extractor = ArxivExtractor(data_dir=data_dir, ensure_data_dir=True)
github_extractor = GitHubExtractor(data_dir=data_dir, ensure_data_dir=True)
playwright_extractor = PlaywrightExtractor(data_dir=data_dir, ensure_data_dir=True)
singlefile_extractor = SingleFileExtractor(
    chrome_config=ChromeSettings(binary=str(detect_playwright_chromium())),
    out_dir=data_dir,
    ensure_out_dir=True,
)
staticfile_extractor = StaticFileExtractor(data_dir=data_dir, ensure_data_dir=True)


# %%
alimiter = AsyncTimeWindowRateLimiter(5, 20)  # 5 requests every 20 seconds


def is_static_file(url: str) -> bool:
    """Determine whether file is static or requires rendering."""
    # TODO: the proper way is with MIME type detection + ext, not only extension
    # see: https://github.com/mikeckennedy/content-types?featured_on=pythonbytes
    pagename = urlparse(url).path.rsplit("/", 1)[-1]
    extension = Path(pagename).suffix.replace(".", "")
    return extension.lower() in STATICFILE_EXTENSIONS


@alimiter
async def scrape(source: Source):
    """Scrape logic."""
    url = source.url

    if is_static_file(url):
        logger.info(f"StaticFileExtractor({url})")
        result = await staticfile_extractor(source)
        if result.scrape_status == ScrapeStatus.COMPLETE:
            return result

    if "arxiv.org" in url:
        logger.info(f"ArxivExtractor({url})")
        result = await arxiv_extractor(source)
        if result.scrape_status == ScrapeStatus.COMPLETE:
            return result

    logger.info(f"SingleFileExtractor({url})")
    result = await singlefile_extractor(source)
    if result.scrape_status == ScrapeStatus.COMPLETE:
        return result

    logger.info(f"PlaywrightExtractor({url})")
    result = await playwright_extractor(source)
    return result


# %%
engine = get_db_engine(
    SQLALCHEMY_DATABASE_URL,
    echo=True,  # for dev
)

initialize_database(engine)

# %%
# urls = [
#     "https://sqlmodel.tiangolo.com/tutorial/insert/#create-a-session",
#     "https://reinforcedknowledge.com/transformers-attention-is-all-you-need/",
#     # Attn is all you need
#     "https://arxiv.org/abs/1706.03762",
#     "https://arxiv.org/html/1706.03762v7",
#     "https://arxiv.org/pdf/1706.03762",
#     # hard to scrape (scrolling background effects, javascript animations/charts)
#     "https://www.bloomberg.com/graphics/2023-generative-ai-bias/",
#     "https://www.washingtonpost.com/technology/interactive/2023/ai-generated-images-bias-racism-sexism-stereotypes/",
#     "https://www.washingtonpost.com/technology/interactive/2024/ai-bias-beautiful-women-ugly-images/",
# ]
urls = [
    "https://adoption.microsoft.com/en-us/project-sophia/",
    "https://www.theregister.com/2024/03/29/microsoft_azure_safety_tools/",
    "https://aider.chat/2024/03/08/claude-3.html",
    "https://www.databricks.com/blog/introducing-dbrx-new-state-art-open-llm",
    "https://www.mov-axbx.com/wopr/wopr_concept.html",
    "https://www.linkedin.com/posts/llamaindex_save-memory-and-money-in-the-rag-activity-7179169269031587840-nVgs",
    "https://www.jdsupra.com/legalnews/utah-passes-artificial-intelligence-1386840/",
    "https://knowingmachines.org/models-all-the-way",
    "https://arxiv.org/abs/2403.16977",
    "https://github.com/ahgraber/homelab-gitops-k3s",
]
add_urls_to_backlog(engine, urls)


# %%
pending = get_pending_sources(engine)
results = []
for source in pending:
    # await scrape(source)
    results.append(synchronize(scrape, source))

# %%
update_scraped_sources(engine, results)

# %%


# %%

# %%

# %%

# %%
from docling.document_converter import DocumentConverter  # NOQA: E402

# %%
# source = "https://arxiv.org/pdf/2408.09869"  # PDF path or URL
# source = "https://arxiv.org/html/2408.09869v3"
source = "https://reinforcedknowledge.com/transformers-attention-is-all-you-need/"
converter = DocumentConverter()
result = converter.convert(source)

print(result.document.export_to_markdown())  # output: "### Docling Technical Report[...]"
