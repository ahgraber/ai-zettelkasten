# %%
# from sqlalchemy import create_engine
# from sqlalchemy.ext.declarative import declarative_base
# from sqlalchemy.orm import Session, sessionmaker
import logging
from pathlib import Path
import typing as t
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, delete, select

from aizk.datamodel.schema import ScrapeStatus, Source, SourceLink, ValidatedURL
from aizk.utilities import path_is_dir
from aizk.utilities.url_helpers import is_social_url

logger = logging.getLogger(__file__)


def get_db_engine(db_url: str, echo: bool = False):
    """Return database engine."""
    return create_engine(db_url, echo=echo)


# create all tables that don't yet exist
def initialize_database(engine: Engine):
    """Create all tables that don't yet exist."""
    SQLModel.metadata.create_all(engine)


def is_supported_site(url: str) -> bool:
    """Determine whether website is supported."""
    if "youtube.com" in url:
        logger.info("YouTube Extraction not yet implemented.")
        return False

    if is_social_url(url):
        logger.info(
            f"Extraction from social media is not supported.  Review {url} and submit referenced content as distinct sources."
        )
        return False

    return True


# TODO: is it expensive to init a new session each time?
def add_urls_to_backlog(engine: Engine, urls: t.List[str]):
    """Add source to database if it does not exist, marked as pending."""
    # add links to db
    with Session(engine) as session:
        for url in urls:
            existing = session.exec(select(Source).where(Source.url == url)).first()
            if existing:
                logger.debug(f"URL {url} already exists in DB, skipping")
                continue
            # else:

            if is_supported_site(url):
                record = Source(url=url)
            else:
                record = Source(url=url, scrape_status=ScrapeStatus.UNSUPPORTED)

            try:
                session.add(record)
            except IntegrityError:
                logger.warning(f"URL {url} already exists in DB; `session.add()` should not have occurred")
        session.commit()
        # session.refresh(record)  # not needed for single record session


def get_pending_sources(engine: Engine):
    """Return all Sources with PENDING status."""
    with Session(engine) as session:
        pending = session.exec(select(Source).where(Source.scrape_status == ScrapeStatus("PENDING"))).all()
        return pending


def update_scraped_sources(
    engine: Engine,
    sources: t.List[Source],
):
    """Update source metadata after successful scrape."""
    with Session(engine) as session:
        for s in sources:
            current = session.exec(select(Source).where(Source.url == s.url)).first()
            if current:
                current.scraped_at = s.scraped_at
                current.scrape_status = s.scrape_status
                current.content_hash = s.content_hash
                current.error_message = s.error_message
                current.file = s.file
                session.add(current)
            else:
                logger.debug(f"URL {s.url} not found in DB")

        session.commit()
        # session.refresh(...) # not needed for single record session


def delete_source(engine: Engine, source: Source, db_dir: Path | str):
    """Delete source from database and file archive."""
    with Session(engine) as session:
        session.exec(delete(Source).where(Source.uuid == source.uuid))
        session.commit()
        # session.refresh(...) # not needed for single record session

    # delete files
    if db_dir := path_is_dir(db_dir):
        # find all child directories named after the source uuid
        dirs = list(db_dir.rglob(str(source.uuid)))
        logger.debug(f"Deleting {len(dirs)} directories for source {source.uuid}")
        for d in dirs:
            d.rmdir()
