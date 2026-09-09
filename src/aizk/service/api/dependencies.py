"""FastAPI request dependencies shared by every aizk application."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlmodel import Session

from fastapi import Request

from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine, get_session
from aizk.service.auth import get_auth_settings

__all__ = ["get_auth_settings", "get_config", "get_db_session"]


def get_config(request: Request) -> Any:
    """Return the shared config instance from application state.

    The return type is opaque because each application puts its own settings
    object on ``app.state.config``.
    """
    return request.app.state.config


def get_db_session() -> Iterator[Session]:
    """Provide a database session for request handling."""
    yield from get_session(get_engine(DatabaseConfig().database_url))
