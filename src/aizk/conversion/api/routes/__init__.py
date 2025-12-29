"""API route modules for conversion service."""

from .bookmarks import router as bookmarks_router
from .health import router as health_router
from .jobs import router as jobs_router
from .outputs import router as outputs_router
from .ui import router as ui_router

__all__ = ["bookmarks_router", "health_router", "jobs_router", "outputs_router", "ui_router"]
