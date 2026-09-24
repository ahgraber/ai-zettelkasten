"""Import-boundary guards for the graph stage descriptors.

The console's stage descriptors must dispatch to *domain* code, never to another
module's FastAPI route internals. The conversion descriptor's boundary is pinned in
``tests/graph/integration/test_console_conversion.py``; these guard the graph stages:
their transition helpers come from each stage's ``job_actions`` (the lifted domain
module), so no descriptor imports from each stage's ``api`` (the route/wiring layer).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import aizk.console.stages.contextualization as contextualization_stage
import aizk.console.stages.extraction as extraction_stage


def _module_imports(module) -> set[str]:
    """Return the fully-qualified module names imported by ``module``'s source."""
    tree = ast.parse(pathlib.Path(module.__file__).read_text())
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
    return imports


@pytest.mark.parametrize(
    ("module", "domain_module", "route_prefixes"),
    [
        (
            contextualization_stage,
            "aizk.contextualization.job_actions",
            ("aizk.graph.api", "aizk.contextualization.api"),
        ),
        (extraction_stage, "aizk.extraction.job_actions", ("aizk.graph.api", "aizk.extraction.api")),
    ],
    ids=["contextualization", "extraction"],
)
def test_graph_descriptor_imports_domain_not_routes(module, domain_module, route_prefixes) -> None:
    """A graph descriptor imports its transitions from the domain module, not a route module."""
    imports = _module_imports(module)

    # No import from the route/wiring layer.
    assert not any(name.startswith(route_prefixes) for name in imports), (
        f"{module.__name__} imports from the route layer: {[n for n in imports if n.startswith(route_prefixes)]}"
    )
    # The transition helpers come from the lifted domain module.
    assert domain_module in imports
