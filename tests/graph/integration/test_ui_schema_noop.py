"""Schema no-op check for the operator console's HTML surfaces.

The schema-tracked OpenAPI is the **conversion** app's, a different app from the
operator console app the HTML mounts on. These tests evidence that the console's
HTML surfaces (dashboard, task monitor, drill-down, actions, explorer) introduce no
tracked-schema delta: being ``include_in_schema=False`` they are absent from the
console app's own OpenAPI, and the console's routes do not appear in the conversion
app's OpenAPI, while the graph JSON API remains present.
"""

from __future__ import annotations

from aizk.conversion.api.main import create_app as create_conversion_app
from aizk.graph.api.main import create_app as create_console_app

#: The console's HTML route prefixes, which must never appear in either OpenAPI.
_CONSOLE_HTML_PREFIXES = ("/ui/tasks", "/ui/explore")


def test_console_html_absent_from_console_app_openapi() -> None:
    """The console's HTML routes are excluded from the console app's own generated schema."""
    paths = create_console_app().openapi()["paths"]

    assert not any(path.startswith("/ui") for path in paths)
    # The JSON API the console sits beside remains in the app's schema.
    assert any(path.startswith("/v1/contextualizations") for path in paths)


def test_console_routes_absent_from_conversion_openapi() -> None:
    """No console HTML route appears in the schema-tracked conversion app OpenAPI."""
    paths = create_conversion_app().openapi()["paths"]

    assert not any(path.startswith(_CONSOLE_HTML_PREFIXES) for path in paths)
