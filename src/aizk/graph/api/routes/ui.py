"""Serve the HTML content explorer for graph operators.

The routes show active chunking and contextualized content, source provenance, and
search results. They require the same principal as the JSON API and keep HTML
routes out of OpenAPI.
"""

from aizk.console.explorer import (
    graph_ui_explorer,
    graph_ui_explorer_detail,
    graph_ui_explorer_search,
    highlight_terms,
    router,
)

__all__ = [
    "graph_ui_explorer",
    "graph_ui_explorer_detail",
    "graph_ui_explorer_search",
    "highlight_terms",
    "router",
]
