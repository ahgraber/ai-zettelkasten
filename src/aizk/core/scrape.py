# %%
import argparse
import asyncio
import datetime
import hashlib
import logging
import os
from pathlib import Path
import re

import dotenv
from sqlmodel import Field, Session, SQLModel, create_engine

from aizk.core.database import (
    add_urls_to_backlog,
    get_db_engine,
    get_pending_sources,
    initialize_database,
    update_scraped_sources,
)
from aizk.datamodel.schema import *
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
from aizk.utilities.parse import (
    URL_REGEX,
    clean_link_title,
    clean_url,
    extract_md_url,
    find_all_urls,
)
from aizk.utilities.path_helpers import path_is_dir, path_is_file

logger = logging.getLogger(__name__)


# %%
def load_urls_from_recent(source_dir: Path, days: int | None) -> list[str]:
    """Identify new URLs from source directory."""
    source_dir = path_is_dir(source_dir)

    if days:
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=days)
        files = [
            file
            for file in source_dir.rglob("*")
            if file.is_file() and datetime.datetime.fromtimestamp(file.stat().st_mtime) > cutoff_date
        ]
    else:
        files = [file for file in source_dir.rglob("*") if file.is_file()]

    urls = []
    for file in files:
        with file.open("r") as f:
            text = f.read()

        urls.extend(find_all_urls(text))

    return urls


# %%
def is_static_file(url: str) -> bool:
    """Determine whether file is static or requires rendering."""
    # TODO: the proper way is with MIME type detection + ext, not only extension
    pagename = urlparse(url).path.rsplit("/", 1)[-1]
    extension = Path(pagename).suffix.replace(".", "")
    return extension.lower() in STATICFILE_EXTENSIONS


# @alimiter
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
if __name__ == "__main__":
    basic_log_config()

    parser = argparse.ArgumentParser(description="Scrape URLs.")
    parser.add_argument("-e", "--env", type=Path, help="Path to a .env file.", default=Path.cwd() / ".aizk.env")
    parser.add_argument("-l", "--last", type=int, help="Consider files changed in last n days", default=7)
    args = parser.parse_args()
    _ = dotenv.load_dotenv(args.env)

    # configure via .env
    sourcedir = Path(os.environ["SOURCE_DIR"])
    try:
        sourcedir = path_is_dir(os.environ["SOURCE_DIR"])
    except FileNotFoundError:
        logger.info(f"Source directory {sourcedir} not found, creating...")
        sourcedir.mkdir(parents=True, exist_ok=True)

    dbdir = Path(os.environ["SOURCE_DIR"])
    try:
        dbdir = path_is_dir(os.environ["DB_DIR"])
    except FileNotFoundError:
        logger.info(f"DB directory {dbdir} not found, creating...")
        dbdir.mkdir(parents=True, exist_ok=True)

    SQLALCHEMY_DATABASE_URL = f"sqlite:///{dbdir}/aizk.db"

    arxiv_extractor = ArxivExtractor(
        out_dir=dbdir / "arxiv",
        ensure_out_dir=True,
    )
    playwright_extractor = PlaywrightExtractor(
        out_dir=dbdir / "playwright",
        ensure_out_dir=True,
    )
    singlefile_extractor = SingleFileExtractor(
        chrome_config=ChromeSettings(binary=str(detect_playwright_chromium())),
        out_dir=dbdir / "singlefile",
        ensure_out_dir=True,
    )
    staticfile_extractor = StaticFileExtractor(
        out_dir=dbdir / "staticfile",
        ensure_out_dir=True,
    )

    alimiter = AsyncTimeWindowRateLimiter(
        int(os.environ.get("LIMITER_REQUESTS", 5)),
        int(os.environ.get("LIMITER_SECONDS", 20)),
    )  # 5 requests every 20 seconds

    alimited_scrape = alimiter(scrape)

    logger.info("Connecting to database...")
    engine = get_db_engine(
        SQLALCHEMY_DATABASE_URL,
        echo=True,  # for dev
    )

    initialize_database(engine)

    logger.info("Identifying new sources...")
    urls = load_urls_from_recent(sourcedir, args.last)
    add_urls_to_backlog(engine, urls)

    logger.info("Scraping sources...")
    pending = get_pending_sources(engine)
    results = []
    for source in pending:
        # await scrape(alimited_scrape)
        # asyncio.gather(alimited_scrape(source)) # TODO???
        results.append(synchronize(alimited_scrape, source))

    logger.info("Updating database...")
    update_scraped_sources(engine, results)
