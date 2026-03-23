"""FastAPI dependencies for database sessions and S3 clients."""

from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session

from aizk.conversion.db import get_session
from aizk.conversion.storage.s3_client import S3Client
from aizk.conversion.utilities.config import ConversionConfig


def get_db_session() -> Iterator[Session]:
    """Provide a database session for request handling."""
    yield from get_session()


def get_s3_client() -> S3Client:
    """Provide an S3Client configured from environment variables."""
    return S3Client(ConversionConfig())
