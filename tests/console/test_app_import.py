"""The operator app root and its old import paths load in any order in a fresh interpreter."""

from __future__ import annotations

import itertools
import subprocess
import sys

import pytest

_MODULES = ("aizk.graph.api", "aizk.graph.api.main", "aizk.console.app")


@pytest.mark.parametrize("order", list(itertools.permutations(_MODULES)), ids="->".join)
def test_the_app_modules_import_in_any_order(order: tuple[str, ...]) -> None:
    """Importing the three app modules in ``order`` completes and serves one ``create_app``."""
    source = "; ".join(
        [
            *(f"import {module}" for module in order),
            "assert aizk.graph.api.create_app is aizk.console.app.create_app",
        ]
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
