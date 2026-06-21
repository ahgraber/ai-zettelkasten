"""FastAPI application setup for the conversion service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from starlette.middleware.trustedhost import TrustedHostMiddleware

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from aizk.conversion.api.routes import bookmarks_router, health_router, jobs_router, outputs_router, ui_router
from aizk.conversion.utilities.config import AuthSettings, ConversionConfig
from aizk.conversion.utilities.dotenv import load_process_dotenv_once
from aizk.conversion.utilities.logging import configure_logging
from aizk.utilities.mlflow_tracing import configure_mlflow_tracing


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize resources needed for the API lifespan."""
    from aizk.conversion.wiring.api import build_api_runtime
    from aizk.db.migrations import run_migrations

    load_process_dotenv_once()
    config = ConversionConfig()
    # AuthSettings construction validates `AIZK_AUTH_MODE`; a reserved-but-unimplemented
    # value (token / proxy_headers / oidc) raises ConfigurationError here, before the
    # HTTP listener binds — startup-time refusal per the deployment trust model.
    auth_settings = AuthSettings()
    _app.state.config = config
    _app.state.auth_settings = auth_settings
    configure_logging(config)
    configure_mlflow_tracing(
        enabled=config.mlflow_tracing_enabled,
        tracking_uri=config.mlflow_tracking_uri,
        experiment_name=config.mlflow_experiment_name,
    )
    run_migrations()
    _api_runtime = build_api_runtime(config)
    _app.state.submission_capabilities = _api_runtime.capabilities
    _app.state.converter_name = _api_runtime.converter_name
    _app.state.converter_config_snapshot = _api_runtime.converter_config_snapshot
    _app.state.docling_config = _api_runtime.docling_config
    yield


def create_app() -> FastAPI:
    """Create the FastAPI application instance."""
    app = FastAPI(title="Docling Conversion Service", lifespan=lifespan)

    # Trusted-host enforcement runs before route handlers; reads the actual `Host`
    # header that reaches this process and rejects on allowlist mismatch (HTTP 400,
    # Starlette default body "Invalid host header"). `Forwarded` / `X-Forwarded-Host`
    # are intentionally NOT consulted — reverse proxies must rewrite Host themselves.
    # NOTE: any future CORS middleware MUST be registered BEFORE this one so CORS
    # preflight succeeds even on Host-mismatch requests.
    trusted_hosts = ConversionConfig().trusted_hosts
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

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
