"""The explorer and shell templates render from the console's template directory."""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from types import SimpleNamespace

import pytest

from aizk.console.rendering import TEMPLATES

_TEMPLATE_NAMES = (
    "_nav.html",
    "_styles_base.html",
    "_styles_jobs.html",
    "explorer.html",
    "explorer_detail.html",
    "explorer_panel.html",
    "explorer_results.html",
)


@pytest.mark.parametrize("name", _TEMPLATE_NAMES)
def test_the_template_renders_from_the_console_directory(name: str) -> None:
    """Each template loads from ``aizk/console/templates`` and renders with a request in context."""
    template = TEMPLATES.env.get_template(name)
    request = SimpleNamespace(url=SimpleNamespace(path="/ui/explore/chunks"))

    rendered = template.render(request=request)

    assert Path(template.filename).parent == Path(str(importlib.resources.files("aizk.console") / "templates"))
    assert rendered.strip()
