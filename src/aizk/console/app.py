"""Build the graph operator FastAPI application.

The lifespan loads object-storage, auth, and admission settings into
``app.state`` for route dependencies.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from aizk.console.dashboard import router as console_dashboard_router
from aizk.console.explorer import router as explorer_router
from aizk.console.routes import router as console_router
from aizk.contextualization.api.routes import router
from aizk.extraction.api.routes import router as extraction_router
from aizk.pipeline.admission import AdmissionConfig
from aizk.service.api import build_app
from aizk.service.auth import AuthSettings
from aizk.service.settings import ServiceSettings
from aizk.storage.settings import ObjectStorageSettings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def lifespan(app: FastAPI) -> "AsyncIterator[None]":
    """Load the object-storage, auth, and admission settings onto application state."""
    app.state.object_storage = ObjectStorageSettings()
    app.state.auth_settings = AuthSettings()
    app.state.admission_config = AdmissionConfig()
    yield


def create_app() -> FastAPI:
    """Build the contextualization operator FastAPI application.

    The operator API mirrors the conversion API perimeter, in case it is served
    on its own listener rather than only behind conversion's. The HTML operator
    surfaces — the descriptor-driven console (dashboard + task monitor +
    drill-down + actions) with the app-root redirect to the dashboard, and the
    content explorer — sit behind that same perimeter and resolve the same
    principal as the JSON API, and are kept out of the generated OpenAPI.
    """
    return build_app(
        title="aizk operator console",
        routers=[
            router,
            extraction_router,
            (console_router, {"include_in_schema": False}),
            (console_dashboard_router, {"include_in_schema": False}),
            (explorer_router, {"include_in_schema": False}),
        ],
        lifespan=lifespan,
        trusted_hosts=ServiceSettings().trusted_hosts,
    )
