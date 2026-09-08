"""Tests for the symlink-refusing open primitive and the write path that depends on its mode."""

from __future__ import annotations

import errno
import os
import stat
from typing import TYPE_CHECKING

import pytest

from aizk.conversion.utilities.paths import write_text_nofollow
from aizk.utilities.paths import open_nofollow

if TYPE_CHECKING:
    from pathlib import Path


def test_open_nofollow_reads_a_regular_file(tmp_path: "Path") -> None:
    """A regular file opens and yields its contents."""
    target = tmp_path / "regular.txt"
    target.write_text("payload", encoding="utf-8")

    fd = open_nofollow(target, os.O_RDONLY)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        assert handle.read() == "payload"


def test_open_nofollow_refuses_a_symlink(tmp_path: "Path") -> None:
    """A symlink leaf is refused rather than followed to its target."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)

    with pytest.raises(OSError) as exc_info:
        open_nofollow(link, os.O_RDONLY)

    assert exc_info.value.errno == errno.ELOOP


def test_open_nofollow_applies_the_requested_mode(tmp_path: "Path") -> None:
    """A creating call gets the mode it asked for, not the umask default."""
    target = tmp_path / "created.txt"
    previous_umask = os.umask(0o000)
    try:
        fd = open_nofollow(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    finally:
        os.umask(previous_umask)
    os.close(fd)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_text_nofollow_keeps_owner_only_permissions(tmp_path: "Path") -> None:
    """The parent-written ``manifest.json`` stays owner-only even under a permissive umask."""
    manifest_path = tmp_path / "manifest.json"

    previous_umask = os.umask(0o000)
    try:
        write_text_nofollow(manifest_path, '{"version": "2.0"}')
    finally:
        os.umask(previous_umask)

    assert manifest_path.read_text(encoding="utf-8") == '{"version": "2.0"}'
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
