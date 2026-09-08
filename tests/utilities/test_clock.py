"""Tests for the shared wall clock."""

from __future__ import annotations

import datetime

from aizk.utilities.clock import utcnow


def test_utcnow_is_timezone_aware_utc() -> None:
    """Every caller relies on the returned value carrying a zero UTC offset."""
    moment = utcnow()

    assert moment.tzinfo is datetime.timezone.utc
    assert moment.utcoffset() == datetime.timedelta(0)


def test_utcnow_tracks_the_wall_clock() -> None:
    """Successive reads move forward, so a claim timestamp orders after an earlier one."""
    first = utcnow()
    second = utcnow()

    assert second >= first
