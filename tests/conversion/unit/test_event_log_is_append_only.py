"""Regression scan: the conversion transition-event log is append-only.

The event log is the durable audit trail; UPDATE / DELETE statements
against the event table would silently corrupt it. Conversion's transition
events now live in the shared ``pipeline_events`` table (relocated from the
former ``conversion_job_events``), so this guard scans ``src/aizk/conversion/``
for patterns that mutate or delete rows on either name and asserts no matches
exist outside the migrations directory (the relocation/teardown migrations carry
the only legitimate INSERT-back / DELETE / DROP statements) and test fixtures.

The patterns checked match the typical SQLAlchemy / SQLModel write
shapes: ``session.delete(<event-bound row>)`` and raw SQL forms
``UPDATE`` / ``DELETE FROM`` against the event tables.
"""

from __future__ import annotations

from pathlib import Path
import re

_CONVERSION_ROOT = Path(__file__).resolve().parents[3] / "src" / "aizk" / "conversion"
_MIGRATIONS_DIR = _CONVERSION_ROOT / "migrations"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"session\.delete\(\s*\w*event\w*"),  # session.delete(event) / session.delete(some_event)
    re.compile(r"UPDATE\s+conversion_job_events", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+conversion_job_events", re.IGNORECASE),
    re.compile(r"UPDATE\s+pipeline_events", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM\s+pipeline_events", re.IGNORECASE),
]


def _is_under_migrations(path: Path) -> bool:
    try:
        path.relative_to(_MIGRATIONS_DIR)
    except ValueError:
        return False
    return True


def test_event_log_has_no_update_or_delete_writes() -> None:
    """No production code path may UPDATE or DELETE the event log."""
    offenders: list[str] = []
    for path in _CONVERSION_ROOT.rglob("*.py"):
        # Alembic migrations carry the only legitimate ``DROP TABLE`` /
        # corresponding statements in their ``downgrade()``; tolerate them.
        if _is_under_migrations(path):
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in _PATTERNS:
                if pattern.search(line):
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
                    break
    assert not offenders, (
        "Conversion transition-event rows must be append-only — no UPDATE / DELETE / "
        "session.delete writes allowed outside the migrations directory. "
        "Offending lines:\n" + "\n".join(offenders)
    )
