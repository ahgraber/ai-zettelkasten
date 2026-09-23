"""Tests for the process bootstrap: where dotenv is loaded, and how often.

Loading ``.env`` is a process-entry act. If an application factory did it, the
first app built in a test session would repopulate every variable a later test
deleted, and in production a uvicorn reload child's request perimeter would come
from a file rather than from the environment the operator started the process
with. These tests pin both halves: which modules load it, and that the guard
lets python-dotenv run once per process.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from aizk.service import bootstrap

#: Modules that may start a process and therefore must put ``.env`` into the
#: environment before anything reads settings.
_COMPOSITION_ROOTS = [
    "aizk.conversion.cli",
    "aizk.graph.cli",
    "aizk.conversion.api.main",
    "aizk.conversion.wiring.subprocess",
]

#: Modules that build an application or a runtime and are reached with the
#: process environment already settled.
_BUILDERS = [
    "aizk.service.api.app",
    "aizk.conversion.wiring.api",
    "aizk.conversion.wiring.worker",
]


def _calls(module: str, function_name: str) -> bool:
    """Report whether ``module``'s source contains a call to ``function_name``."""
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin is not None, f"Cannot locate module {module!r}"
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == function_name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == function_name:
            return True
    return False


def test_the_shared_process_preamble_loads_dotenv() -> None:
    """The preamble every CLI runs is what puts ``.env`` into the process environment."""
    assert _calls("aizk.service.cli", "load_process_dotenv_once")


@pytest.mark.parametrize("module", _COMPOSITION_ROOTS)
def test_every_composition_root_loads_dotenv(module: str) -> None:
    """A process entrypoint loads dotenv itself or through the shared preamble."""
    assert _calls(module, "load_process_dotenv_once") or _calls(module, "prepare_process"), (
        f"{module} must load dotenv — it is a process composition root."
    )


@pytest.mark.parametrize("module", _BUILDERS)
def test_no_application_builder_loads_dotenv(module: str) -> None:
    """A builder must not load dotenv: it runs inside a process someone else started."""
    assert not _calls(module, "load_dotenv")
    assert not _calls(module, "load_process_dotenv_once")
    assert not _calls(module, "prepare_process")


def test_the_guard_lets_python_dotenv_run_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls to the guarded loader read ``.env`` once."""
    calls: list[bool] = []
    monkeypatch.setattr(bootstrap, "_DOTENV_LOADED", False)
    monkeypatch.setattr(bootstrap, "load_dotenv", lambda: calls.append(True))

    bootstrap.load_process_dotenv_once()
    bootstrap.load_process_dotenv_once()

    assert calls == [True]
