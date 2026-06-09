"""Low-level database helpers shared across the graph stage.

Holds the ``BEGIN IMMEDIATE`` session context manager used by both the
unit-of-work's persist transaction and the contextualization memo's autonomous
upserts. Keeping it here lets the memo writer open its own short immediate
transaction without ``persistence`` importing ``workunit`` internals (which would
invert the workunit → persistence dependency direction).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlmodel import Session

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine


@contextlib.contextmanager
def begin_immediate(engine: "Engine") -> "Iterator[Session]":
    """Open a ``BEGIN IMMEDIATE`` session; commit on success, roll back on error.

    Acquires the single serialized writer's lock up front so a writer never
    deadlocks on a lock upgrade. The transaction is held only for the duration of
    the ``with`` block, so callers must keep that block free of slow work (e.g. a
    model call) to avoid serializing every other writer.
    """
    session = Session(engine)
    try:
        session.exec(text("BEGIN IMMEDIATE"))
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
