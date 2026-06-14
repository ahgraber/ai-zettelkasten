"""Schema no-op check for the graph operator UI.

The schema-tracked OpenAPI is the **conversion** app's, a different app from the
graph operator app the UI mounts on. These tests evidence that adding the graph UI
router introduces no tracked-schema delta: the UI paths are absent from the
conversion app's OpenAPI, and (being ``include_in_schema=False``) absent from the
graph app's own OpenAPI as well, while the graph JSON API remains present.
"""

from __future__ import annotations

from aizk.conversion.api.main import create_app as create_conversion_app
from aizk.graph.api.main import create_app as create_graph_app

_GRAPH_UI_PREFIX = "/ui/graph"


def test_graph_ui_router_absent_from_conversion_openapi() -> None:
    """No graph UI path appears in the schema-tracked conversion app OpenAPI."""
    paths = create_conversion_app().openapi()["paths"]

    assert not any(path.startswith(_GRAPH_UI_PREFIX) for path in paths)


def test_graph_ui_router_absent_from_graph_app_openapi() -> None:
    """The graph UI routes are excluded from the graph app's own generated schema."""
    paths = create_graph_app().openapi()["paths"]

    assert not any(path.startswith(_GRAPH_UI_PREFIX) for path in paths)
    # The JSON API the UI sits beside remains in the graph app's schema.
    assert any(path.startswith("/v1/contextualizations") for path in paths)
