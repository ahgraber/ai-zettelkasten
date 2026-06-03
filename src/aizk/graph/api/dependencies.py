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
from aizk.conversion.utilities.config import ConversionConfig

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlmodel import Session


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
