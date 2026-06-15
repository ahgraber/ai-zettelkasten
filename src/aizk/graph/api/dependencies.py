"""FastAPI dependencies for the contextualization operator API.

Reuses the conversion service's cached engine and session helpers (the graph
tables live in the conversion database), reading the shared configuration from
``app.state`` so tests can override either the configuration or the session
dependency directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from aizk.conversion.db import get_engine, get_session
from aizk.conversion.storage.s3_client import S3Client
from aizk.conversion.utilities.config import ConversionConfig
from aizk.graph.search import Fts5SearchProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlmodel import Session

    from aizk.graph.markdown_source import BlobReader
    from aizk.graph.search import SearchProvider


def get_config(request: Request) -> ConversionConfig:
    """Return the shared configuration instance from application state.

    ``Request`` is imported at runtime (not under ``TYPE_CHECKING``) so FastAPI
    can resolve the annotation and inject the request rather than mistaking it for
    a query parameter.
    """
    return request.app.state.config


def get_db_session(request: Request) -> "Iterator[Session]":
    """Yield a request-scoped session on the shared conversion database engine."""
    config = get_config(request)
    yield from get_session(get_engine(config.database_url))


def get_blob_reader(request: Request) -> "BlobReader":
    """Return the production blob reader for the explorer's on-demand markdown read.

    Builds the conversion stage's :class:`~aizk.conversion.storage.s3_client.S3Client`
    from the shared configuration; it satisfies the
    :class:`~aizk.graph.markdown_source.BlobReader` protocol via ``get_object_bytes``.
    Injected (not constructed inline in the route) so a test overrides it with an
    in-memory fake and the explorer never reaches real S3.
    """
    return S3Client(get_config(request))


def get_search_provider(request: Request) -> "SearchProvider":
    """Return the content search provider for the explorer's search-results view.

    Builds an FTS5-backed :class:`~aizk.graph.search.Fts5SearchProvider` over the
    shared conversion database engine. Injected behind the
    :class:`~aizk.graph.search.SearchProvider` protocol so a test overrides it and so
    the relevance backend is a swap with no route change.
    """
    config = get_config(request)
    return Fts5SearchProvider(get_engine(config.database_url))
