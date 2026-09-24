"""Import-surface lock for the ``aizk.graph`` package."""

from __future__ import annotations

import subprocess
import sys

# Runs in a fresh interpreter: imports the package and prints which stage
# modules it pulled into ``sys.modules``.
_IMPORT_RUNNER = (
    "import sys\n"
    "import aizk.graph\n"
    "stage_modules = ('aizk.graph.contextualization', 'aizk.graph.handler', 'aizk.graph.workunit',"
    " 'aizk.ai.providers.llm', 'aizk.contextualization')\n"
    "print(sorted(name for name in stage_modules if name in sys.modules))\n"
)

# Runs in a fresh interpreter: reads two stage names off the package and prints
# whether each is the object its defining module holds.
_ACCESS_RUNNER = (
    "import aizk.graph\n"
    "stage_handler = aizk.graph.ContextualizationStageHandler\n"
    "summarize = aizk.graph.summarize_document\n"
    "from aizk.graph import contextualization, handler\n"
    "print(stage_handler is handler.ContextualizationStageHandler)\n"
    "print(summarize is contextualization.summarize_document)\n"
)


def _run(source: str) -> str:
    """Run ``source`` in a fresh interpreter and return its stripped stdout."""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_package_import_loads_no_stage_module() -> None:
    """Importing ``aizk.graph`` loads none of the stage modules it re-exports from."""
    assert _run(_IMPORT_RUNNER) == "[]"


def test_stage_names_resolve_on_access() -> None:
    """A stage name read off ``aizk.graph`` is the object its defining module holds."""
    assert _run(_ACCESS_RUNNER).splitlines() == ["True", "True"]
