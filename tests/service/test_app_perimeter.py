"""The trusted-host perimeter a uvicorn worker inherits from a serve command.

A serve command populates the process environment, then hands uvicorn an import
string. A reload child imports that string in the inherited environment, so the
allowlist enforced on its requests is whatever the environment said at the
moment the application was constructed.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import os
from pathlib import Path
from unittest import mock

import pytest
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fastapi import FastAPI


def _uvicorn_target(cli_module: str) -> tuple[str, bool]:
    """Return the import string and the ``factory`` flag the module hands to uvicorn."""
    spec = importlib.util.find_spec(cli_module)
    assert spec is not None and spec.origin is not None
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "run" or not isinstance(node.func.value, ast.Name) or node.func.value.id != "uvicorn":
            continue
        factory = any(
            keyword.arg == "factory" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
            for keyword in node.keywords
        )
        return node.args[0].value, factory
    raise AssertionError(f"{cli_module} hands no import string to uvicorn.run")


def _allowed_hosts(app: FastAPI) -> list[str]:
    """Return the allowlist the application's trusted-host middleware enforces."""
    for middleware in app.user_middleware:
        if middleware.cls is TrustedHostMiddleware:
            return middleware.kwargs["allowed_hosts"]
    raise AssertionError("the application registers no trusted-host middleware")


@pytest.mark.parametrize("cli_module", ["aizk.conversion.cli", "aizk.graph.cli"])
def test_the_serve_target_takes_its_allowlist_from_the_process_environment(cli_module: str) -> None:
    """Resolving the serve command's import string yields the environment's allowlist."""
    import_string, factory = _uvicorn_target(cli_module)
    module_name, _, attribute = import_string.partition(":")

    with mock.patch.dict(os.environ, {"AIZK_TRUSTED_HOSTS": '["api.example.internal"]'}):
        module = importlib.import_module(module_name)
        # A non-factory string names an application built at import time, so the
        # child's construction is reproduced by re-executing the module.
        app = getattr(module, attribute)() if factory else getattr(importlib.reload(module), attribute)
        allowed = _allowed_hosts(app)

    if not factory:
        importlib.reload(importlib.import_module(module_name))

    assert allowed == ["api.example.internal"]
