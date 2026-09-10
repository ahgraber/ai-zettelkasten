"""Compatibility re-export of the exclusive-claim primitive at its former graph path.

The primitive lives in :mod:`aizk.db.engine`; this module serves import
statements only.
"""

from __future__ import annotations

from aizk.db.engine import begin_immediate

__all__ = ["begin_immediate"]
