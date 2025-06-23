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
from aizk.utilities.path_utils import path_is_dir
from aizk.utilities.url_utils import is_social_url

logger = logging.getLogger(__file__)


def get_db_engine(db_url: str, echo: bool = False):
    """Return database engine."""
    return create_engine(db_url, echo=echo)


# create all tables that don't yet exist
def initialize_database(engine: Engine):
    """Create all tables that don't yet exist."""
    SQLModel.metadata.create_all(engine)
