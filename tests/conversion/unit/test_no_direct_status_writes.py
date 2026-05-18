"""Regression scan: every ConversionJob.status write must funnel through record_transition.

This guard exists because the durable event log is only valid if every
status mutation is paired with an event row. ``record_transition`` is the
sole helper that performs the paired write atomically; any other assignment
to ``.status`` produces a silent projection-only mutation that is invisible
to the audit trail.

The check is a textual scan rather than an AST walk: it is intentionally
strict (matches anything that looks like ``<attr>.status = ConversionJobStatus.``)
and intentionally narrow in scope (only files inside
``src/aizk/conversion/``). A false positive should be addressed by routing
the assignment through ``record_transition``; a deliberately-allowed
assignment must live inside ``datamodel/events.py``.
"""

from __future__ import annotations

from pathlib import Path
import re

_CONVERSION_ROOT = Path(__file__).resolve().parents[3] / "src" / "aizk" / "conversion"
_ALLOWED_FILE = _CONVERSION_ROOT / "datamodel" / "events.py"
_PATTERN = re.compile(r"\.status\s*=\s*ConversionJobStatus\.")
# Tolerate the helper's own internal mutation via ``to_status`` parameter
# pattern (``job.status = to_status``) — the regex above does not match it.


def test_no_direct_conversion_job_status_writes_outside_events_module() -> None:
    """Every ``.status = ConversionJobStatus.*`` write lives in ``events.py``."""
    offenders: list[str] = []
    for path in _CONVERSION_ROOT.rglob("*.py"):
        if path == _ALLOWED_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Direct ConversionJob.status writes outside record_transition are forbidden — "
        "route through aizk.conversion.datamodel.events.record_transition instead. "
        "Offending lines:\n" + "\n".join(offenders)
    )
