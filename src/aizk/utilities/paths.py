"""Filesystem primitives for opening a path without following a symlink."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def open_nofollow(path: "Path | str", flags: int, mode: int = 0o777) -> int:
    """Open ``path`` with ``O_NOFOLLOW`` added, returning the file descriptor.

    Mirrors :func:`os.open`, including its ``mode`` default, so a call site that
    creates a file keeps the permissions it asked for. The refusal surfaces as the
    platform's raw ``OSError`` (``ELOOP`` for a symlink leaf, ``EEXIST`` when the
    caller also passes ``O_EXCL``); translating those into a domain error and
    logging the attempt belong to the caller, which knows what the path is for.

    Args:
        path: Filesystem path to open.
        flags: ``os.open`` flags; ``O_NOFOLLOW`` is added.
        mode: Permission bits applied when the flags create the file.

    Returns:
        The open file descriptor, which the caller owns and must close.

    Raises:
        OSError: As :func:`os.open` raises it, untranslated.
    """
    return os.open(str(path), flags | os.O_NOFOLLOW, mode)
