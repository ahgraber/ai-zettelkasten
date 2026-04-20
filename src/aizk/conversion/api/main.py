"""FastAPI application setup for the conversion service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from aizk.conversion.api.routes import bookmarks_router, health_router, jobs_router, outputs_router, ui_router
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.logging import configure_logging
from aizk.utilities.mlflow_tracing import configure_mlflow_tracing


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize resources needed for the API lifespan."""
    from dotenv import load_dotenv

    from aizk.conversion.migrations import run_migrations
    from aizk.conversion.wiring.api import build_api_runtime

    load_dotenv()
    config = ConversionConfig()
    _app.state.config = config
    configure_logging(config)
    configure_mlflow_tracing(
        enabled=config.mlflow_tracing_enabled,
        tracking_uri=config.mlflow_tracking_uri,
        experiment_name=config.mlflow_experiment_name,
    )
    run_migrations()
    _api_runtime = build_api_runtime(config)
    _app.state.submission_capabilities = _api_runtime.capabilities
    _app.state.docling_config = _api_runtime.docling_config
    yield


def create_app() -> FastAPI:
    """Create the FastAPI application instance."""
    app = FastAPI(title="Docling Conversion Service", lifespan=lifespan)
    app.include_router(health_router)
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
