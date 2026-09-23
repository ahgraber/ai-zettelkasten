"""Tests for the graph table registration in the shared SQLModel metadata."""

from __future__ import annotations

from collections import Counter

from sqlmodel import SQLModel

import aizk.graph.datamodel as datamodel


def _table_classes() -> list[type[SQLModel]]:
    return [
        obj
        for name in datamodel.__all__
        if isinstance(obj := getattr(datamodel, name), type)
        and issubclass(obj, SQLModel)
        and hasattr(obj, "__table__")
    ]


def test_every_graph_table_class_is_registered_once_in_the_shared_metadata() -> None:
    """Each exported table class owns exactly one metadata table, and the graph tables are exactly those."""
    classes = _table_classes()
    tablenames = Counter(cls.__tablename__ for cls in classes)
    graph_tables = {name for name in SQLModel.metadata.tables if name.startswith("graph_")}

    assert len(classes) == 16
    assert all(count == 1 for count in tablenames.values())
    assert set(tablenames) == graph_tables
    for cls in classes:
        assert SQLModel.metadata.tables[cls.__tablename__] is cls.__table__
