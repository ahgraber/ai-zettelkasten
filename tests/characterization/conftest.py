"""Reuse the conversion suite's hermetic app harness for the route-body pins.

The environment snapshot, the migration-built SQLite database, and the session
come from ``tests/conversion/conftest.py`` so the characterization tests drive the
same apps the integration suite does, on the same database URL, without reading
``.env``.
"""

from __future__ import annotations

from tests.conversion.conftest import (  # noqa: F401
    _ensure_repo_on_path,
    _hermetic_conversion_config,
    _restore_socket_stubs,
    db_engine,
    db_session,
    set_test_env,
    test_db_path,
)
