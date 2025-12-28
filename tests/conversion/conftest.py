"""Shared fixtures for conversion service tests."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from sqlmodel import Session

from aizk.db import create_db_and_tables, get_engine


@pytest.fixture(scope="session")
def test_db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a temp SQLite path for test database storage."""
    return tmp_path_factory.mktemp("conversion_db") / "conversion_service.db"


@pytest.fixture(autouse=True)
def set_test_env(monkeypatch: pytest.MonkeyPatch, test_db_path: Path) -> None:
    """Ensure tests use a temp SQLite database and predictable settings."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{test_db_path}")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "0")


@pytest.fixture()
def db_engine(test_db_path: Path):
    """Create and initialize a SQLite engine for tests."""
    engine = get_engine(f"sqlite:///{test_db_path}")
    create_db_and_tables(engine)
    return engine


@pytest.fixture()
def db_session(db_engine) -> Iterator[Session]:
    """Provide a SQLModel session tied to the test database."""
    with Session(db_engine) as session:
        yield session
