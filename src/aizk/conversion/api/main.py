"""FastAPI application setup for the conversion service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from aizk.conversion.api.routes import bookmarks_router, health_router, jobs_router, outputs_router
from aizk.conversion.settings import ConversionConfig
from aizk.service.api import build_app
from aizk.service.auth import AuthSettings
from aizk.service.bootstrap import load_process_dotenv_once
from aizk.service.logging import configure_logging
from aizk.service.settings import ServiceSettings
from aizk.storage.settings import ObjectStorageSettings
from aizk.utilities.tracing import configure_mlflow_tracing


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize resources needed for the API lifespan."""
    from aizk.conversion.wiring.api import build_api_runtime
    from aizk.db.migrations import run_migrations

    load_process_dotenv_once()
    config = ConversionConfig()
    # A uvicorn reload child runs the lifespan without going through the CLI, so
    # the process-level configuration is applied here rather than inherited.
    settings = ServiceSettings()
    object_storage = ObjectStorageSettings()
    # AuthSettings construction validates `AIZK_AUTH_MODE`; a reserved-but-unimplemented
    # value (token / proxy_headers / oidc) raises ConfigurationError here, before the
    # HTTP listener binds — startup-time refusal per the deployment trust model.
    auth_settings = AuthSettings()
    _app.state.config = config
    _app.state.object_storage = object_storage
    _app.state.auth_settings = auth_settings
    configure_logging(settings)
    configure_mlflow_tracing(settings.mlflow)
    run_migrations()
    _api_runtime = build_api_runtime(config)
    _app.state.submission_capabilities = _api_runtime.capabilities
    _app.state.converter_name = _api_runtime.converter_name
    _app.state.converter_config_snapshot = _api_runtime.converter_config_snapshot
    _app.state.docling_config = _api_runtime.docling_config
    yield


def create_app() -> FastAPI:
    """Create the FastAPI application instance."""
    app = build_app(
        title="Docling Conversion Service",
        routers=[health_router, jobs_router, bookmarks_router, outputs_router],
        lifespan=lifespan,
        trusted_hosts=ServiceSettings().trusted_hosts,
    )

    @app.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        """Redirect root to the API docs; the conversion service serves only JSON.

        Operator HTML lives in the console app; this service exposes the JSON API
        and its OpenAPI docs.
        """
        return RedirectResponse(url="/docs", status_code=307)

    return app


app = create_app()
