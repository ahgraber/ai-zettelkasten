"""Tests for the graph table registration in the shared SQLModel metadata."""

from __future__ import annotations

from collections import Counter

from sqlalchemy.orm import configure_mappers
from sqlmodel import SQLModel

from aizk.conversion.datamodel import Source
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


def test_conversion_and_graph_models_configure_with_one_mapper_per_class_name() -> None:
    """Conversion and graph models share one registry that maps each class name exactly once."""
    configure_mappers()
    names = Counter(mapper.class_.__name__ for mapper in SQLModel._sa_registry.mappers)

    assert all(names[cls.__name__] == 1 for cls in [Source, *_table_classes()])
    assert [name for name, count in names.items() if count > 1] == []
