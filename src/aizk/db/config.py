"""Shared database configuration and deterministic backend selection.

The active backend is a function of the configured ``database_url`` scheme,
surfaced as an explicit :class:`DatabaseBackend` identity. Selection fails closed:
a URL whose backend has no implemented arm raises at configuration time rather
than silently defaulting to another backend or proceeding half-configured.
"""

from __future__ import annotations

from enum import Enum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class DatabaseBackend(str, Enum):
    """A supported database backend, identified by the database URL scheme."""

    SQLITE = "sqlite"


class UnsupportedDatabaseBackendError(RuntimeError):
    """Raised when the configured database URL names an unsupported backend.

    Covers both an unrecognized scheme and a backend whose arm is planned but not
    yet implemented (for example ``postgresql://`` before its backend lands).
    """


_SCHEME_TO_BACKEND: dict[str, DatabaseBackend] = {
    DatabaseBackend.SQLITE.value: DatabaseBackend.SQLITE,
}


def resolve_backend(database_url: str) -> DatabaseBackend:
    """Return the backend identity for a database URL, failing closed.

    Raises:
        UnsupportedDatabaseBackendError: when the URL's scheme names a backend
            that has no implemented arm.
    """
    scheme = make_url(database_url).get_backend_name()
    backend = _SCHEME_TO_BACKEND.get(scheme)
    if backend is None:
        supported = ", ".join(sorted(_SCHEME_TO_BACKEND)) or "(none)"
        raise UnsupportedDatabaseBackendError(
            f"database backend '{scheme}' is not supported; supported backends: {supported}"
        )
    return backend


class DatabaseConfig(BaseSettings):
    """Stage-independent database configuration shared by every stage.

    Reads the unchanged ``AIZK_DATABASE_URL`` environment variable and exposes a
    read-only :attr:`backend` derived from the URL scheme.
    """

    model_config = SettingsConfigDict(env_prefix="AIZK_", env_file=None, extra="ignore")

    database_url: str = "sqlite:///./data/conversion_service.db"

    @model_validator(mode="after")
    def _fail_closed_on_unsupported_backend(self) -> "DatabaseConfig":
        """Resolve the backend at construction so an unsupported one fails closed."""
        resolve_backend(self.database_url)
        return self

    @property
    def backend(self) -> DatabaseBackend:
        """Return the active backend identity derived from :attr:`database_url`."""
        return resolve_backend(self.database_url)
