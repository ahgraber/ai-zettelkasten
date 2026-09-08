"""The wall clock every stage reads for a timestamp it records or compares."""

from __future__ import annotations

import datetime


def utcnow() -> datetime.datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    Awareness is the property callers depend on: a naive value compared against
    an aware one raises, and a value serialized into an event payload renders its
    offset.

    Returns:
        The current instant with ``tzinfo`` set to :data:`datetime.timezone.utc`.
    """
    return datetime.datetime.now(datetime.timezone.utc)
