"""Provide FastAPI dependencies for the graph operator API.

External readers and search providers remain injectable for tests and route
composition. The shared providers are re-exported from
:mod:`aizk.service.api.dependencies` as the same objects, so a dependency
override keyed on either path resolves the same callable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine
from aizk.graph.search import Fts5SearchProvider
from aizk.service.api.dependencies import get_db_session, get_object_storage
from aizk.storage.s3 import S3Client

if TYPE_CHECKING:
    from aizk.contextualization.processing.markdown_source import BlobReader
    from aizk.graph.config import AdmissionConfig
    from aizk.graph.search import SearchProvider

__all__ = [
    "get_admission_config",
    "get_blob_reader",
    "get_db_session",
    "get_object_storage",
    "get_search_provider",
]


def get_admission_config(request: Request) -> "AdmissionConfig":
    """Return the graph admission settings from application state."""
    return request.app.state.admission_config


def get_blob_reader(request: Request) -> "BlobReader":
    """Return the production blob reader for source Markdown."""
    return S3Client(get_object_storage(request))


def get_search_provider(_request: Request) -> "SearchProvider":
    """Return the content search provider for the explorer."""
    return Fts5SearchProvider(get_engine(DatabaseConfig().database_url))
