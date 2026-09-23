"""Import-cost lock for the conversion composition roots."""

from __future__ import annotations

import subprocess
import sys

import pytest

# Runs in a fresh interpreter: imports one composition root and prints which of
# the heavy converter dependencies it pulled into ``sys.modules``.
_SUBPROCESS_RUNNER = (
    "import importlib, sys\n"
    "importlib.import_module(%r)\n"
    "print(sorted(name for name in ('torch', 'docling') if name in sys.modules))\n"
)


@pytest.mark.parametrize("module", ["aizk.conversion.cli", "aizk.conversion.api.main"])
def test_composition_root_import_leaves_docling_and_torch_unloaded(module: str) -> None:
    """Importing a composition root loads neither ``torch`` nor ``docling``."""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _SUBPROCESS_RUNNER % module],
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == "[]"
