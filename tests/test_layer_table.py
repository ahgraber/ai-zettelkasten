"""Enforce the package layer table over every import in ``src/aizk``.

The table below says, per package, which packages it may import. Anything else is
a violation and must appear in :data:`RECORDED_EXCEPTIONS` with the change that
removes it. The check runs in both directions: a new edge outside the table fails,
and an exception that is no longer a violation fails, so the ledger cannot drift
away from the tree.

Composition roots wire the layers together and are therefore exempt as importers.
Rows for packages that do not exist yet are present and inert.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

_SRC = Path(__file__).resolve().parents[1] / "src"
_ROOT_PACKAGE = "aizk"

#: Package → the packages it may import, named relative to ``aizk``. A target is
#: permitted when it equals an entry or is nested under it, so a row may name a
#: sub-package (``conversion.datamodel``) to admit only that slice. A package may
#: always import itself. Empty means "nothing inside aizk".
LAYER_TABLE: dict[str, frozenset[str]] = {
    # Helpers with no aizk dependency.
    "utilities": frozenset(),
    # Engine, config, backends, migrations.
    "db": frozenset({"utilities"}),
    # Object storage client and its settings.
    "storage": frozenset({"utilities"}),
    # Process bootstrap: dotenv, logging, tracing, settings, auth, app factory.
    "service": frozenset({"db", "utilities"}),
    # Runner, handler protocol, status vocabulary, events, runs, identity.
    "pipeline": frozenset({"db", "utilities"}),
    # Model provider clients and their config.
    "ai.providers": frozenset({"utilities"}),
    # Splitter and chunk model.
    "chunking": frozenset({"pipeline", "utilities"}),
    # The conversion stage.
    "conversion": frozenset({"pipeline", "service", "storage", "ai.providers", "db", "chunking", "utilities"}),
    # The artifact store: tables, chunk and mention stores, fingerprints, search.
    "graph": frozenset({"chunking", "pipeline", "db", "utilities", "conversion.datamodel"}),
    # One stage each, laid out like conversion; they never import each other.
    "contextualization": frozenset(
        {
            "graph",
            "pipeline",
            "service",
            "storage",
            "ai.providers",
            "db",
            "chunking",
            "utilities",
            "conversion.datamodel",
        }
    ),
    "extraction": frozenset(
        {
            "graph",
            "pipeline",
            "service",
            "storage",
            "ai.providers",
            "db",
            "chunking",
            "utilities",
            "conversion.datamodel",
        }
    ),
    # Operator pages over all stages, the explorer, the operator app root.
    "console": frozenset(
        {
            "graph",
            "contextualization",
            "extraction",
            "conversion",
            "chunking",
            "pipeline",
            "service",
            "storage",
            "ai.providers",
            "db",
            "utilities",
        }
    ),
    # Unwired leaves: the claimify prompts, the OCR metrics, and the note-graph
    # prototype reach nothing else inside aizk.
    "ai": frozenset(),
    "ai.graph": frozenset(),
    "metrics": frozenset(),
    # The note-graph prototype's remaining halves. Nothing else imports them; both
    # rows disappear when the prototype is folded into ``ai.graph``.
    "core": frozenset({"datamodel", "utilities"}),
    "datamodel": frozenset(),
}

#: Modules that wire the layers together. A root may import anything; a trailing
#: ``.*`` matches every module in that package.
COMPOSITION_ROOTS: frozenset[str] = frozenset(
    {
        "aizk.conversion.cli",
        "aizk.graph.cli",
        "aizk.conversion.api.main",
        "aizk.console.app",
        "aizk.graph.api.main",
        "aizk.conversion.wiring.*",
        "aizk.contextualization.wiring.*",
        "aizk.extraction.wiring.*",
        "aizk.db.migrations.env",
    }
)


class Edge(NamedTuple):
    """One import statement that crosses a package boundary.

    Attributes:
        importer: Dotted module the statement is written in.
        imported: Dotted module the statement names.
    """

    importer: str
    imported: str


#: Every edge the table forbids today, with the change that removes it. Asserted
#: to equal the scan exactly, so neither a new violation nor a resolved one can
#: pass unnoticed.
RECORDED_EXCEPTIONS: dict[Edge, str] = {
    Edge("aizk.graph", "aizk.ai.providers.llm"): (
        "removed when the contextualization stage moves to aizk.contextualization"
    ),
    Edge("aizk.graph.api.dependencies", "aizk.service.api.dependencies"): (
        "removed when the graph API dependencies move into the stage packages"
    ),
    Edge("aizk.graph.api.dependencies", "aizk.storage.s3"): (
        "removed when the graph API dependencies move into the stage packages"
    ),
    Edge("aizk.graph.api.routes", "aizk.service.api.responses"): (
        "removed when the contextualization routes move to aizk.contextualization"
    ),
    Edge("aizk.graph.api.routes", "aizk.service.auth"): (
        "removed when the contextualization routes move to aizk.contextualization"
    ),
    Edge("aizk.graph.api.routes.extraction", "aizk.service.api.responses"): (
        "removed when the extraction routes move to aizk.extraction"
    ),
    Edge("aizk.graph.api.routes.extraction", "aizk.service.auth"): (
        "removed when the extraction routes move to aizk.extraction"
    ),
    Edge("aizk.graph.api.routes.ui", "aizk.service.auth"): "removed when the content explorer moves to aizk.console",
    Edge("aizk.graph.contextualization", "aizk.ai.providers.llm"): (
        "removed when the contextualization stage moves to aizk.contextualization"
    ),
    Edge("aizk.graph.handler", "aizk.ai.providers.llm"): (
        "removed when the contextualization stage moves to aizk.contextualization"
    ),
    Edge("aizk.graph.llm", "aizk.ai.providers.llm"): (
        "removed when the compatibility re-exports at the old module paths are removed"
    ),
    Edge("aizk.graph.markdown_source", "aizk.storage"): (
        "removed when the Markdown source moves to aizk.contextualization"
    ),
    Edge("aizk.graph.worker", "aizk.ai.providers.llm"): (
        "removed when the contextualization worker moves to aizk.contextualization"
    ),
    Edge("aizk.graph.worker", "aizk.conversion.utilities.config"): (
        "removed when the worker stops taking a conversion config and is handed object-storage settings"
    ),
    Edge("aizk.graph.worker", "aizk.storage.s3"): (
        "removed when the contextualization worker moves to aizk.contextualization"
    ),
    Edge("aizk.graph.workunit", "aizk.ai.providers.llm"): (
        "removed when the contextualization stage moves to aizk.contextualization"
    ),
    Edge("aizk.utilities.batch_utils", "aizk.ai.providers.batch"): (
        "removed when the compatibility re-exports at the old module paths are removed"
    ),
    Edge("aizk.utilities.cli_gates", "aizk.service.cli"): (
        "removed when the compatibility re-exports at the old module paths are removed"
    ),
}

#: Import directions the table refuses outright: a layer reaching a package above
#: it. None may appear in the scan, ledger or not, because recording one would
#: invert the dependency the layout exists to establish.
FORBIDDEN_DIRECTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("db", "service"),
        ("db", "storage"),
        ("ai.providers", "service"),
        ("ai.providers", "graph"),
    }
)


def _module_name(path: Path) -> str:
    """Return the dotted module name for a file under ``src``."""
    parts = list(path.relative_to(_SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(module: str) -> str | None:
    """Return the layer-table row a dotted module belongs to, longest row first."""
    if not module.startswith(_ROOT_PACKAGE + "."):
        return None
    relative = module[len(_ROOT_PACKAGE) + 1 :]
    matches = [row for row in LAYER_TABLE if relative == row or relative.startswith(row + ".")]
    return max(matches, key=len) if matches else None


def _is_root(module: str) -> bool:
    """Report whether ``module`` is a composition root."""
    for pattern in COMPOSITION_ROOTS:
        if pattern.endswith(".*"):
            if module.startswith(pattern[:-1]):
                return True
        elif module == pattern:
            return True
    return False


def _is_module(dotted: str) -> bool:
    """Report whether a dotted aizk name is a module or package on disk.

    The directory is listed and the name compared exactly, because a
    case-insensitive filesystem would otherwise resolve an imported class name
    (``Principal``) onto a sibling module file (``principal.py``).
    """
    *parents, leaf = dotted.split(".")
    directory = _SRC.joinpath(*parents)
    if not directory.is_dir():
        return False
    names = {entry.name for entry in directory.iterdir()}
    return f"{leaf}.py" in names or (leaf in names and (directory / leaf / "__init__.py").is_file())


def _absolute_targets(node: ast.Import | ast.ImportFrom, module: str, is_package: bool) -> list[str]:
    """Return the dotted aizk modules one import statement names.

    In ``from X import name``, ``name`` may be either a symbol or a sub-module. The
    two are told apart by looking for the sub-module on disk, so a statement that
    reaches a permitted slice through its parent package (``from aizk.conversion
    import datamodel``) is read as the slice rather than as the parent.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names if alias.name.split(".")[0] == _ROOT_PACKAGE]

    if node.level:
        anchor = module.split(".")
        if not is_package:
            anchor = anchor[:-1]
        anchor = anchor[: len(anchor) - (node.level - 1)]
        base = ".".join([*anchor, node.module] if node.module else anchor)
    else:
        base = node.module or ""
    if base.split(".")[0] != _ROOT_PACKAGE:
        return []

    targets = []
    for alias in node.names:
        nested = f"{base}.{alias.name}"
        targets.append(nested if _is_module(nested) else base)
    return targets


def _scan() -> set[Edge]:
    """Return every cross-package import edge in ``src/aizk``, roots excluded."""
    edges: set[Edge] = set()
    for path in sorted((_SRC / _ROOT_PACKAGE).rglob("*.py")):
        module = _module_name(path)
        if _is_root(module):
            continue
        source = _package_of(module)
        if source is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _absolute_targets(node, module, path.name == "__init__.py"):
                if _package_of(target) not in (None, source):
                    edges.add(Edge(module, target))
    return edges


def _is_permitted(edge: Edge) -> bool:
    """Report whether the table admits ``edge``."""
    source = _package_of(edge.importer)
    relative = edge.imported[len(_ROOT_PACKAGE) + 1 :]
    allowed = LAYER_TABLE[source] if source is not None else frozenset()
    return any(relative == entry or relative.startswith(entry + ".") for entry in allowed)


def test_layer_table_has_a_row_for_every_package_in_the_tree() -> None:
    """No module in the tree falls outside the table, so nothing is silently unchecked."""
    unmapped = {
        _module_name(path)
        for path in (_SRC / _ROOT_PACKAGE).rglob("*.py")
        if _module_name(path) != _ROOT_PACKAGE and _package_of(_module_name(path)) is None
    }

    assert unmapped == set()


def test_every_import_edge_is_permitted_or_a_recorded_exception() -> None:
    """A cross-package import the table forbids must be a known, described exception."""
    violations = {edge for edge in _scan() if not _is_permitted(edge)}

    assert violations - set(RECORDED_EXCEPTIONS) == set()


def test_every_recorded_exception_is_still_a_violation() -> None:
    """An exception that no longer describes a real edge must be deleted, not carried."""
    violations = {edge for edge in _scan() if not _is_permitted(edge)}

    assert set(RECORDED_EXCEPTIONS) - violations == set()


def test_every_recorded_exception_names_the_change_that_removes_it() -> None:
    """Each entry says what makes it go away, so the ledger is actionable on its own."""
    assert all(reason.startswith("removed when ") for reason in RECORDED_EXCEPTIONS.values())


def test_the_shared_infrastructure_rows_hold_modules() -> None:
    """The bootstrap, object-storage, and provider rows describe code, not intentions."""
    populated = {_package_of(_module_name(path)) for path in (_SRC / _ROOT_PACKAGE).rglob("*.py")}

    assert {"service", "storage", "ai.providers"} <= populated


def test_no_layer_imports_a_package_above_it() -> None:
    """The engine, the object store, and the provider clients sit below what they serve.

    These directions are refused rather than recorded: an entry in the ledger would
    let a cycle live in the tree for as long as the ledger carries it.
    """
    directions = {(_package_of(edge.importer), _package_of(edge.imported)) for edge in _scan()}

    assert directions & FORBIDDEN_DIRECTIONS == set()


def test_console_reads_of_the_graph_stage_are_permitted_not_exceptions() -> None:
    """The console may import every stage, so its graph reads are recorded, never refused."""
    console_to_graph = [
        edge for edge in _scan() if _package_of(edge.importer) == "console" and _package_of(edge.imported) == "graph"
    ]

    assert all(_is_permitted(edge) for edge in console_to_graph)
    assert not any(edge in RECORDED_EXCEPTIONS for edge in console_to_graph)
