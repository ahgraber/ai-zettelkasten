"""FastAPI dependencies for database sessions and S3 clients."""

from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session

from fastapi import Request

from aizk.conversion.db import get_engine, get_session
from aizk.conversion.storage.s3_client import S3Client
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.wiring.capabilities import DeploymentCapabilities


def get_config(request: Request) -> ConversionConfig:
    """Return the shared config instance from application state."""
    return request.app.state.config


def get_db_session(request: Request) -> Iterator[Session]:
    """Provide a database session for request handling."""
    config = get_config(request)
    yield from get_session(get_engine(config.database_url))


def get_s3_client(request: Request) -> S3Client:
    """Provide an S3Client configured from application state."""
    return S3Client(get_config(request))


def get_capabilities(request: Request) -> DeploymentCapabilities:
    """Return the DeploymentCapabilities built by the API lifespan."""
    return request.app.state.api_runtime.capabilities
