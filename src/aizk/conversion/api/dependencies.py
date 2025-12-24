"""FastAPI dependencies for database sessions and S3 clients."""

from __future__ import annotations

from collections.abc import Iterator

from boto3.session import Session as BotoSession
from sqlmodel import Session

from aizk.conversion.utilities.config import ConversionConfig
from aizk.db import get_session


def get_db_session() -> Iterator[Session]:
    """Provide a database session for request handling."""
    yield from get_session()


def get_s3_client():
    """Provide an S3 client configured from environment variables."""
    config = ConversionConfig()
    session = BotoSession(
        aws_access_key_id=config.s3_access_key_id,
        aws_secret_access_key=config.s3_secret_access_key,
        region_name=config.s3_region,
    )
    return session.client("s3", endpoint_url=config.s3_endpoint_url or None)
