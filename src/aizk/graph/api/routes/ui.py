"""HTML UI routes for the graph operator surface.

These routes are HTMX server-rendered like the conversion operator UI and are
mounted on the graph operator app (``graph/api/main.py``) behind its
``TrustedHostMiddleware`` perimeter. Every route resolves the request
:class:`~aizk.conversion.auth.principal.Principal` through the same
:func:`~aizk.conversion.api.dependencies.get_principal` dependency the graph JSON
API uses, so the UI is not a weaker perimeter than the API beside it. The routes
are declared ``include_in_schema=False`` where mounted, keeping HTML endpoints out
of the generated OpenAPI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates

from aizk.conversion.api.dependencies import get_principal
from aizk.conversion.auth import Principal

router = APIRouter(prefix="/ui/graph", tags=["ui"])

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))


@router.get("/jobs")
def graph_ui_jobs(
    request: Request,
    _principal: Annotated[Principal, Depends(get_principal)],
):
    """Render the contextualization jobs page (full page or ``HX-Request`` partial)."""
    template = "jobs_panel.html" if request.headers.get("HX-Request") else "jobs.html"
    return _TEMPLATES.TemplateResponse(request, template, {})
