import asyncio
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import aiofiles

logger = logging.getLogger(__name__)


def to_valid_fname(fname: str) -> str:
    r"""Convert a string to a valid filename.

    Removes or replaces characters that are invalid on any major OS:
    - Windows: < > : " / \\ | ? *
    - Leading/trailing spaces and dots
    - Control characters

    Args:
    fname: The filename to sanitize.

    Returns:
    A valid cross-platform filename.
    """
    import re

    # Replace invalid characters with hyphen
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        fname = fname.replace(char, "-")

    # Remove control characters (ASCII 0-31)
    fname = "".join(c if ord(c) >= 32 else "_" for c in fname)

    # Strip leading/trailing spaces and dots (Windows doesn't allow these)
    fname = fname.strip(" .")

    # Replace spaces with underscores
    fname = fname.replace(" ", "_")

    # Replace consecutive hyphens/underscores with a single one
    fname = fname.replace("-_", "-")
    fname = re.sub(r"[-_]+", lambda m: m.group(0)[0], fname)

    # Ensure it's not empty after sanitization
    if not fname:
        raise ValueError("Filename is empty or contains only invalid characters.")

    # Limit length (255 bytes is common limit, leave some margin)
    if len(fname.encode("utf-8")) > 192:
        raise ValueError("Filename is too long after sanitization.")

    return fname


class AtomicWriter:
    """Class that provides both sync and async context managers for atomic writes."""

    def __init__(self, filepath: Path | str, binary_mode: bool = False):
        self.filepath = Path(filepath)
        self.binary_mode = binary_mode
        self.dirpath = self.filepath.parent
        self.fname = self.filepath.name

        if not self.dirpath.is_dir():
            logger.info(f"{self.dirpath} does not exist, creating.")
            self.dirpath.mkdir(parents=True, exist_ok=True)

    def __enter__(self):  # NOQA: D105
        # return self.sync_context().__enter__()
        self.tmpfile = NamedTemporaryFile(
            mode="wb" if self.binary_mode else "w",
            dir=self.dirpath,
            prefix=self.fname,
            suffix=".tmp",
            delete_on_close=False,
        )
        return self.tmpfile

    def __exit__(self, *args):  # NOQA: D105
        # return self.sync_context().__exit__(*args)
        self.tmpfile.flush()  # libc -> OS
        os.fsync(self.tmpfile.fileno())  # OS -> disk
        self.tmpfile.close()

        os.replace(self.tmpfile.name, self.filepath)

    async def __aenter__(self):  # NOQA: D105
        # return self.sync_context().__enter__()
        self.tmpfile = NamedTemporaryFile(  # NOQA: SIM115
            mode="wb" if self.binary_mode else "w",
            dir=self.dirpath,
            prefix=self.fname,
            suffix=".tmp",
            delete_on_close=False,
        )
        self.async_tmpfile = await aiofiles.open(self.tmpfile.name, "wb" if self.binary_mode else "w")
        return self.async_tmpfile

    async def __aexit__(self, *args):  # NOQA: D105
        # return self.sync_context().__exit__(*args)
        await self.async_tmpfile.flush()
        await asyncio.to_thread(os.fsync, self.tmpfile.fileno())
        await self.async_tmpfile.close()
        self.tmpfile.close()

        await asyncio.to_thread(os.replace, self.tmpfile.name, self.filepath)
