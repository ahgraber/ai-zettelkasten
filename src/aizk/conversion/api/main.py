"""FastAPI application setup for the conversion service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.logging import configure_logging
from aizk.db import create_db_and_tables


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize resources needed for the API lifespan."""
    config = ConversionConfig()
    configure_logging(config)
    create_db_and_tables()
    yield


def create_app() -> FastAPI:
    """Create the FastAPI application instance."""
    return FastAPI(title="Docling Conversion Service", lifespan=lifespan)


app = create_app()
