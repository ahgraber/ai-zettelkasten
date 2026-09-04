"""Provide FastAPI dependencies for the graph operator API.

Dependencies reuse the conversion database and shared application configuration.
External readers and search providers remain injectable for tests and route
composition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from aizk.conversion.storage.s3_client import S3Client
from aizk.conversion.utilities.config import ConversionConfig
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine, get_session
from aizk.graph.search import Fts5SearchProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlmodel import Session

    from aizk.graph.config import AdmissionConfig
    from aizk.graph.markdown_source import BlobReader
    from aizk.graph.search import SearchProvider


def get_config(request: Request) -> ConversionConfig:
    """Return the shared configuration from application state."""
    return request.app.state.config


def get_admission_config(request: Request) -> "AdmissionConfig":
    """Return the graph admission settings from application state."""
    return request.app.state.admission_config


def get_db_session(_request: Request) -> "Iterator[Session]":
    """Yield a request-scoped session on the shared database engine."""
    yield from get_session(get_engine(DatabaseConfig().database_url))


def get_blob_reader(request: Request) -> "BlobReader":
    """Return the production blob reader for source Markdown."""
    return S3Client(get_config(request))


def get_search_provider(_request: Request) -> "SearchProvider":
    """Return the content search provider for the explorer."""
    return Fts5SearchProvider(get_engine(DatabaseConfig().database_url))
