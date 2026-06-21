"""The database is a shared, stage-independent foundation (`pluggable-database`).

Two checks for the foundation contract: the graph stage resolves the engine,
migration runner, and database config from ``aizk.db`` rather than importing
another stage's database module, and one migration tree produces every stage's
tables.
"""

import ast
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect

from aizk.db.engine import _ENGINE_CACHE, get_engine
from aizk.db.migrations import run_migrations

# Database modules that used to live under the conversion stage; the graph stage
# must not reach into these (they no longer exist, but the guard pins the contract
# so a future reintroduction under a stage is caught).
_FORBIDDEN_STAGE_DB_IMPORTS = ("aizk.conversion.db", "aizk.conversion.migrations")

_GRAPH_DB_CONSUMERS = (
    "aizk.graph.worker",
    "aizk.graph.cli",
    "aizk.graph.api.dependencies",
)


def _module_source(dotted: str) -> str:
    spec = importlib.util.find_spec(dotted)
    assert spec is not None and spec.origin is not None, f"cannot locate module {dotted!r}"
    return Path(spec.origin).read_text(encoding="utf-8")


def _imported_modules(source: str) -> set[str]:
    """Return the set of fully-qualified module names imported by the source."""
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("module", _GRAPH_DB_CONSUMERS)
def test_graph_resolves_database_from_shared_foundation(module: str) -> None:
    """Graph DB consumers import the engine/migrations/config from ``aizk.db`` only."""
    imports = _imported_modules(_module_source(module))

    leaked = imports & set(_FORBIDDEN_STAGE_DB_IMPORTS)
    assert not leaked, f"{module} imports a stage's database module instead of aizk.db: {sorted(leaked)}"

    assert any(name == "aizk.db" or name.startswith("aizk.db.") for name in imports), (
        f"{module} does not resolve the database from the shared aizk.db foundation"
    )


def test_one_migration_tree_governs_every_stage_table(tmp_path) -> None:
    """Running the shared tree to head produces conversion, graph, and pipeline tables."""
    url = f"sqlite:///{tmp_path / 'all_stages.db'}"
    run_migrations(url)
    engine = get_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
        _ENGINE_CACHE.pop(url, None)

    assert {"sources", "conversion_jobs", "conversion_outputs"}.issubset(tables)  # conversion stage
    assert {"graph_chunks", "graph_contextualization_jobs"}.issubset(tables)  # graph stage
    assert {"pipeline_runs", "pipeline_events"}.issubset(tables)  # shared pipeline
