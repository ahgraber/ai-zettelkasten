"""FastAPI application factory for the contextualization operator API.

The lifespan stores the shared :class:`~aizk.conversion.utilities.config.ConversionConfig`
on ``app.state`` (the graph stage reuses the conversion database); routes read it
through :func:`~aizk.graph.api.dependencies.get_config`. Environment is taken from
the process — the deployment exports it, or the ``aizk-graph`` CLI loads it — so
the app does not read ``.env`` here and tests stay hermetic.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from starlette.middleware.trustedhost import TrustedHostMiddleware

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from aizk.console.routes import router as console_router
from aizk.conversion.utilities.config import AuthSettings, ConversionConfig
from aizk.graph.api.routes import router
from aizk.graph.api.routes.extraction import router as extraction_router
from aizk.graph.api.routes.extraction_ui import router as extraction_ui_router
from aizk.graph.api.routes.ui import router as ui_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def lifespan(app: FastAPI) -> "AsyncIterator[None]":
    """Load the shared configuration and auth settings onto application state."""
    app.state.config = ConversionConfig()
    app.state.auth_settings = AuthSettings()
    yield


def create_app() -> FastAPI:
    """Build the contextualization operator FastAPI application."""
    app = FastAPI(title="aizk contextualization operator API", lifespan=lifespan)
    # Mirror the conversion API perimeter: enforce the trusted-host allowlist before
    # route handlers run (HTTP 400 on a Host-header mismatch), in case the graph
    # operator API is served on its own listener rather than only behind conversion's.
    # Reads the shared ConversionConfig allowlist; reverse proxies must rewrite Host.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ConversionConfig().trusted_hosts)
    app.include_router(router)
    app.include_router(extraction_router)
    # HTML operator UI: behind the same trusted-host perimeter and resolving the same
    # principal as the JSON API. Kept out of the generated OpenAPI — it serves HTML.
    app.include_router(ui_router, include_in_schema=False)
    app.include_router(extraction_ui_router, include_in_schema=False)
    # Descriptor-driven operator console (task monitor + drill-down + actions),
    # behind the same perimeter and principal. Serves HTML, so kept out of OpenAPI.
    app.include_router(console_router, include_in_schema=False)

    @app.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        """Redirect the app root to the contextualization jobs UI landing page."""
        return RedirectResponse(url="/ui/graph/jobs", status_code=307)

    return app
