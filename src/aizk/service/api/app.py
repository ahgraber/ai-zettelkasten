"""Build a FastAPI application with the shared request perimeter already in place."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.middleware.trustedhost import TrustedHostMiddleware

from fastapi import APIRouter, FastAPI

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from starlette.types import Lifespan


def build_app(
    title: str,
    routers: "Sequence[APIRouter | tuple[APIRouter, Mapping[str, Any]]]",
    *,
    lifespan: "Lifespan[FastAPI]",
    trusted_hosts: list[str],
) -> FastAPI:
    """Return an application titled ``title`` serving ``routers`` in the order given.

    Trusted-host enforcement is registered before any router, so it runs before
    route handlers: it reads the actual `Host` header that reaches this process
    and rejects on allowlist mismatch (HTTP 400, Starlette default body
    "Invalid host header"). `Forwarded` / `X-Forwarded-Host` are intentionally
    NOT consulted — reverse proxies must rewrite Host themselves. Any future
    CORS middleware MUST be registered BEFORE the trusted-host middleware so
    CORS preflight succeeds even on Host-mismatch requests.

    Args:
        title: The application title, which is also the OpenAPI document title.
        routers: The routers to include, each either an ``APIRouter`` or a
            ``(router, kwargs)`` pair whose keyword arguments are passed to
            ``FastAPI.include_router``.
        lifespan: The startup/shutdown context manager for the application.
        trusted_hosts: The `Host` header allowlist enforced on every request.

    Returns:
        The configured application.
    """
    app = FastAPI(title=title, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    for entry in routers:
        router, options = entry if isinstance(entry, tuple) else (entry, {})
        app.include_router(router, **options)
    return app
