"""FastAPI application setup for the conversion service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from aizk.conversion.api.routes import bookmarks_router, jobs_router, outputs_router, ui_router
from aizk.conversion.db import create_db_and_tables
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.logging import configure_logging
from aizk.utilities.mlflow_tracing import configure_mlflow_tracing


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize resources needed for the API lifespan."""
    config = ConversionConfig()
    configure_logging(config)
    configure_mlflow_tracing(
        enabled=config.mlflow_tracing_enabled,
        tracking_uri=config.mlflow_tracking_uri,
        experiment_name=config.mlflow_experiment_name,
    )
    create_db_and_tables()
    yield


def create_app() -> FastAPI:
    """Create the FastAPI application instance."""
    app = FastAPI(title="Docling Conversion Service", lifespan=lifespan)
    app.include_router(jobs_router)
    app.include_router(bookmarks_router)
    app.include_router(outputs_router)
    app.include_router(ui_router)

    @app.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        """Temporary shim redirecting root to the jobs UI."""
        return RedirectResponse(url="/ui/jobs", status_code=307)

    return app


app = create_app()
