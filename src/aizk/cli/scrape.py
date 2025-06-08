# %%
import argparse
import asyncio
import datetime
import hashlib
from itertools import batched
import logging
import os
from pathlib import Path
import re
import sys

import dotenv
from sqlmodel import Field, Session, SQLModel, create_engine
from tqdm.asyncio import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

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
from aizk.utilities import (
    LOG_FMT,
    SlidingWindowRateLimiter,
    basic_log_config,
    # logging_redirect_tqdm,
    path_is_dir,
    path_is_file,
    process_manager,
)
from aizk.utilities.url_helpers import find_all_urls, is_social_url

logger = logging.getLogger()
formatter = logging.Formatter(LOG_FMT)
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(formatter)
logger.addHandler(handler)


def load_urls_from_recent(source_dir: Path, days: int | None) -> list[str]:
    """Identify new URLs from source directory."""
    extensions = {".md", ".txt"}
    source_dir = path_is_dir(source_dir)

    def _is_recent(file: Path) -> bool:
        return datetime.datetime.fromtimestamp(file.stat().st_mtime) > cutoff_date

    def _is_valid_file(file: Path) -> bool:
        return file.is_file() and file.suffix in extensions

    if days:
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=days)
        files = [file for file in source_dir.rglob("*") if _is_recent(file) and _is_valid_file(file)]
    else:
        files = [file for file in source_dir.rglob("*") if _is_valid_file(file)]

    urls = []
    for file in files:
        with file.open("r") as f:
            try:
                text = f.read()
            except Exception:
                logger.exception(f"Failed reading {file=}")

        urls.extend(find_all_urls(text))

    return list(set(urls))


def is_static_file(url: str) -> bool:
    """Determine whether file is static or requires rendering."""
    # TODO: the proper way is with MIME type detection + ext, not only extension
    # see: https://github.com/mikeckennedy/content-types?featured_on=pythonbytes
    pagename = urlparse(url).path.rsplit("/", 1)[-1]
    extension = Path(pagename).suffix.replace(".", "")
    return extension.lower() in STATICFILE_EXTENSIONS


async def scrape(source: Source):
    """Scrape logic."""
    url = source.url

    if "youtube.com" in url:
        logger.info("YouTube Extraction not yet implemented.")
        return source

    if is_social_url(url):
        logger.info(
            f"Extraction from social media is not supported.  Review {url} and submit referenced content as distinct sources."
        )
        return source

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


async def run(pending: t.Sequence[Source], limiter: SlidingWindowRateLimiter) -> list[Source]:
    """Run scraping tasks."""
    scrape_with_limiter = limiter(scrape)
    results = []

    with process_manager("chromium"), process_manager("zsh"):
        results = await tqdm.gather(
            *[scrape_with_limiter(source) for source in pending],
            position=1,
            leave=False,
        )
        # results = [
        #     await coro
        #     for coro in tqdm.as_completed(
        #         [scrape_with_limiter(source) for source in pending], total=len(pending), position=1, leave=False
        #     )
        # ]

    return results


# %%
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape URLs.")
    parser.add_argument("-e", "--env", type=Path, help="Path to a .env file.", default=Path.cwd() / ".aizk.env")
    parser.add_argument("-l", "--last", type=int, help="Consider files changed in last n days", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    config = dotenv.dotenv_values(args.env)

    # configure via .env
    source_dir = Path(config["SOURCE_DIR"])
    try:
        source_dir = path_is_dir(config["SOURCE_DIR"])
    except FileNotFoundError:
        logger.info(f"Source directory {source_dir} not found")  # , creating...")
        # source_dir.mkdir(parents=True, exist_ok=True)

    app_dir = Path(config["APP_DIR"])
    try:
        app_dir = path_is_dir(config["APP_DIR"])
    except FileNotFoundError:
        logger.info(f"DB directory {app_dir} not found, creating...")
        app_dir.mkdir(parents=True, exist_ok=True)

    archive_dir = app_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    SQLALCHEMY_DATABASE_URL = f"sqlite:///{app_dir}/aizk.db"

    arxiv_extractor = ArxivExtractor(data_dir=archive_dir)
    gh_extractor = GitHubExtractor(data_dir=archive_dir)
    playwright_extractor = PlaywrightExtractor(data_dir=archive_dir)
    singlefile_extractor = SingleFileExtractor(
        chrome_config=ChromeSettings(binary=str(detect_playwright_chromium())), data_dir=archive_dir
    )
    staticfile_extractor = StaticFileExtractor(data_dir=archive_dir)

    alimiter = SlidingWindowRateLimiter(
        int(config.get("LIMITER_REQUESTS", 5)),
        int(config.get("LIMITER_SECONDS", 20)),
    )  # 5 requests every 20 seconds

    logger.info("Connecting to database...")
    engine = get_db_engine(
        SQLALCHEMY_DATABASE_URL,
        echo=args.verbose,  # for dev
    )

    initialize_database(engine)

    logger.info("Identifying new sources...")
    urls = load_urls_from_recent(source_dir, args.last)
    add_urls_to_backlog(engine, urls)

    logger.info("Scraping sources...")
    pending = get_pending_sources(engine)

    with logging_redirect_tqdm():
        for batch in batched(tqdm(pending, position=0, leave=True), 100):
            results = asyncio.run(run(batch, alimiter))

            logger.info("Updating database...")
            update_scraped_sources(engine, results)
