"""Regression scan: every ConversionJob status write must funnel through record_transition.

This guard exists because the durable event log is only valid if every
status mutation is paired with an event row. After the conversion -> pipeline
runner port the in-process engine was deleted; conversion now runs on
``StageRunner`` + ``ConversionStageHandler`` and status transitions flow
through the audited ``record_transition`` write path.

Two functions perform the sanctioned, event-paired status mutation, and the
scan allow-lists exactly these locations:

- ``aizk.conversion.datamodel.events.record_transition`` validates the
  conversion-specific typed payload, then mutates ``job.status = to_status``
  and stages the matching :class:`~aizk.pipeline.events.PipelineEvent` row in
  the same session (``src/aizk/conversion/datamodel/events.py``).
- ``aizk.pipeline.events.record_transition`` is the generic single write path
  shared across stages: it mutates the work-unit's stage-specific status via
  ``setattr(work_unit, status_attr, to_status)`` and co-commits the audit row
  (``src/aizk/pipeline/events.py``).

Any other assignment to ``.status`` (or ``setattr(..., "status"/status_attr, ...)``)
inside the scanned roots produces a silent projection-only mutation that is
invisible to the audit trail and must be routed through ``record_transition``.

The check is a textual scan rather than an AST walk: it is intentionally
strict (matches anything that looks like a status assignment) and intentionally
narrow in scope. Two regions are deliberately out of scope: the ``PipelineRun``
generation-lifecycle write in ``pipeline/run.py`` (not a work-unit status
transition but a separate, independently-audited ``activate_run`` primitive),
and the Alembic ``migrations/`` trees (raw SQL DDL/DML carried as Python string
literals such as ``cj.status = 'SUCCEEDED'``, never ORM status mutations).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re

_CONVERSION_ROOT = Path(importlib.util.find_spec("aizk.conversion").origin).resolve().parent
_PIPELINE_ROOT = Path(importlib.util.find_spec("aizk.pipeline").origin).resolve().parent

#: The two functions that perform the audited, event-paired status mutation.
#: Every other status write inside the scanned roots is forbidden.
_ALLOWED_FILES = frozenset(
    {
        _CONVERSION_ROOT / "datamodel" / "events.py",
        _PIPELINE_ROOT / "events.py",
    }
)

#: Files scanned for rogue status writes. The conversion root holds every
#: ConversionJob status path; the pipeline root is scanned only for the generic
#: ``record_transition`` write path (``events.py``) and the runner/handler
#: that drive conversion, while ``pipeline/run.py``'s ``PipelineRun`` lifecycle
#: write is left out of scope (different work-unit, separate audited primitive).
_SCANNED_ROOTS = (_CONVERSION_ROOT, _PIPELINE_ROOT)

#: A status assignment in any of:
#:   ``job.status = ...`` / ``self.status = ...``  (attribute assignment)
#:   ``setattr(x, "status", ...)`` / ``setattr(x, status_attr, ...)`` (dynamic)
# Excludes equality comparisons (``.status ==``) via the negative lookahead.
_ATTR_ASSIGN = re.compile(r"\.status\s*=(?!=)")
_SETATTR = re.compile(r"""setattr\([^,]+,\s*(?:["']status["']|status_attr)\s*,""")

# ``pipeline/run.py`` mutates ``PipelineRun.status`` via ``activate_run`` — a
# separate, independently-audited generation-lifecycle primitive, not a
# ConversionJob/work-unit status transition. Exclude it from the scan so the
# guard stays focused on the ``record_transition`` invariant.
_OUT_OF_SCOPE_FILES = frozenset({_PIPELINE_ROOT / "run.py"})


def _is_migration(path: Path) -> bool:
    """Return whether ``path`` lives in an Alembic ``migrations`` tree.

    Migration modules carry raw SQL as Python string literals (e.g.
    ``sa.text("status = 'active'")``), never ORM status mutations, so they are
    excluded from the status-write scan.
    """
    return "migrations" in path.parts


def _scan_files() -> list[Path]:
    """Return the Python files scanned for direct status writes."""
    files: list[Path] = []
    for root in _SCANNED_ROOTS:
        for path in root.rglob("*.py"):
            if path in _ALLOWED_FILES or path in _OUT_OF_SCOPE_FILES or _is_migration(path):
                continue
            files.append(path)
    return files


def test_no_direct_status_writes_outside_record_transition() -> None:
    """Every work-unit status mutation funnels through ``record_transition``.

    Scans the conversion and pipeline source roots and fails if any status
    assignment lives outside the two audited helper modules
    (``conversion/datamodel/events.py`` and ``pipeline/events.py``). A rogue
    ``job.status = X`` or ``setattr(job, "status", X)`` added anywhere else
    bypasses the event-paired write path and is rejected here.
    """
    offenders: list[str] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _ATTR_ASSIGN.search(line) or _SETATTR.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Direct status writes outside the audited record_transition path are forbidden — "
        "route through aizk.conversion.datamodel.events.record_transition (conversion) or "
        "aizk.pipeline.events.record_transition (generic) so the status change is "
        "co-committed with its pipeline_events row. Offending lines:\n" + "\n".join(offenders)
    )
