import asyncio
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import aiofiles

logger = logging.getLogger(__name__)


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
