"""Unit tests for deterministic, fail-closed database backend selection.

Covers the `pluggable-database` contract: the active backend is derived from the
configured database URL and an unsupported backend fails closed at config
resolution.
"""

import pytest
from sqlalchemy import text

from aizk.db.config import (
    DatabaseBackend,
    DatabaseConfig,
    UnsupportedDatabaseBackendError,
    resolve_backend,
)
from aizk.db.engine import _ENGINE_CACHE, get_engine


def test_sqlite_url_selects_sqlite_backend_and_engine_is_usable(tmp_path) -> None:
    """A supported (SQLite) URL resolves to the SQLite backend and yields a usable engine."""
    url = f"sqlite:///{tmp_path / 'sel.db'}"
    config = DatabaseConfig(_env_file=None, database_url=url)

    assert config.backend is DatabaseBackend.SQLITE
    assert resolve_backend(url) is DatabaseBackend.SQLITE

    engine = get_engine(config.database_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()
        _ENGINE_CACHE.pop(url, None)


@pytest.mark.parametrize(
    ("url", "expected_scheme"),
    [
        ("postgresql://user:pw@localhost/db", "postgresql"),  # planned-but-not-implemented
        ("mysql://user:pw@localhost/db", "mysql"),  # unknown/unsupported scheme
    ],
    ids=["not-yet-implemented", "unknown-scheme"],
)
def test_unsupported_backend_fails_closed_at_resolution(url: str, expected_scheme: str) -> None:
    """An unsupported backend URL fails closed at config resolution, naming the backend."""
    with pytest.raises(UnsupportedDatabaseBackendError, match=expected_scheme):
        DatabaseConfig(_env_file=None, database_url=url)

    with pytest.raises(UnsupportedDatabaseBackendError, match=expected_scheme):
        resolve_backend(url)
