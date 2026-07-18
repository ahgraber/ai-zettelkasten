"""Shared Jinja2 template environment for the console's HTML surfaces.

The dashboard and the task-monitor routes render from one environment, so the
template search path — the console's own templates, with the graph package's shell
partials (``_nav.html`` / ``_styles_*``) resolvable as a fallback — is defined in a
single place.
"""

from __future__ import annotations

import importlib.resources

from fastapi.templating import Jinja2Templates

#: The console's templates, with the graph package's shell partials as a fallback.
TEMPLATES = Jinja2Templates(
    directory=[
        str(importlib.resources.files("aizk.console") / "templates"),
        str(importlib.resources.files("aizk.graph") / "templates"),
    ]
)
