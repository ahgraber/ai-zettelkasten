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
    ("get_auth_settings", "aizk.conversion.api.dependencies", "aizk.service.auth"),
    ("get_config", "aizk.conversion.api.dependencies", "aizk.service.api.dependencies"),
    ("get_db_session", "aizk.conversion.api.dependencies", "aizk.service.api.dependencies"),
    ("get_object_storage", "aizk.conversion.api.dependencies", "aizk.service.api.dependencies"),
    ("get_db_session", "aizk.graph.api.dependencies", "aizk.service.api.dependencies"),
    ("get_object_storage", "aizk.graph.api.dependencies", "aizk.service.api.dependencies"),
    ("Principal", "aizk.conversion.auth", "aizk.service.auth"),
    ("Principal", "aizk.conversion.auth.principal", "aizk.service.auth"),
    ("AuthSettings", "aizk.conversion.utilities.config", "aizk.service.auth"),
    ("QueueFullResponse", "aizk.conversion.api.schemas", "aizk.service.api.responses"),
    ("QueueFullResponse", "aizk.conversion.api.schemas.jobs", "aizk.service.api.responses"),
    ("INTAKE_RESPONSES", "aizk.graph.api.routes", "aizk.service.api.responses"),
    ("queue_full_response", "aizk.graph.api.routes", "aizk.service.api.responses"),
    ("ConfigurationError", "aizk.conversion.core.errors", "aizk.utilities.errors"),
    ("StartupValidationError", "aizk.conversion.utilities.startup", "aizk.utilities.errors"),
]


@pytest.mark.parametrize(("name", "alias_path", "home_path"), SHARED_OBJECTS)
def test_both_paths_name_the_same_object(name: str, alias_path: str, home_path: str) -> None:
    """Importing a shared name from either module yields one object, not a copy."""
    alias = getattr(importlib.import_module(alias_path), name)
    home = getattr(importlib.import_module(home_path), name)

    assert alias is home
