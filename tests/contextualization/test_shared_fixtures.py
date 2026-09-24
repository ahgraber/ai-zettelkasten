"""The graph and contextualization test trees build their databases from one table list."""

from __future__ import annotations

import importlib

import tests._graphdb


def test_each_conftest_binds_the_shared_table_list() -> None:
    """Each conftest's graph table list is the shared object, so a new table reaches every tree."""
    conftests = [
        importlib.import_module(name) for name in ("tests.graph.conftest", "tests.contextualization.conftest")
    ]

    assert all(module._GRAPH_SCHEMA_TABLES is tests._graphdb._GRAPH_SCHEMA_TABLES for module in conftests)
