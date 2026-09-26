"""Pin that every shared object resolves to one object, whichever path names it.

FastAPI keys ``app.dependency_overrides`` by the callable itself, and pydantic
models are compared by class, so a path that handed back a wrapper or a copy
would silently stop an override or an ``except`` clause from matching.
"""

from __future__ import annotations

import importlib

import pytest

#: ``(name, path that also exports it, path that defines it)``.
SHARED_OBJECTS: list[tuple[str, str, str]] = [
    ("get_principal", "aizk.conversion.api.dependencies", "aizk.service.auth"),
    ("get_config", "aizk.conversion.api.dependencies", "aizk.service.api.dependencies"),
    ("get_db_session", "aizk.conversion.api.dependencies", "aizk.service.api.dependencies"),
    ("get_object_storage", "aizk.conversion.api.dependencies", "aizk.service.api.dependencies"),
]


@pytest.mark.parametrize(("name", "alias_path", "home_path"), SHARED_OBJECTS)
def test_both_paths_name_the_same_object(name: str, alias_path: str, home_path: str) -> None:
    """Importing a shared name from either module yields one object, not a copy."""
    alias = getattr(importlib.import_module(alias_path), name)
    home = getattr(importlib.import_module(home_path), name)

    assert alias is home
