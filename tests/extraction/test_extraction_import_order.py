"""The extraction routes import cleanly before anything else in a fresh interpreter."""

from __future__ import annotations

import subprocess
import sys


def test_the_routes_module_imports_first_in_a_fresh_interpreter() -> None:
    """Importing ``aizk.extraction.api.routes`` first completes without an import cycle."""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import aizk.extraction.api.routes"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
