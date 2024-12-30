"""SinglefileExtractor.

- ref: https://github.com/ArchiveBox/ArchiveBox/blob/dev/archivebox/pkgs/abx-plugin-singlefile/abx_plugin_singlefile/singlefile.py
- ref: https://github.com/sissbruecker/linkding/blob/master/bookmarks/services/singlefile.py
"""

import asyncio
import datetime
import itertools
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any, List, Tuple, override

from pydantic import ConfigDict, Field, TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict

from aizk.datamodel.schema import ScrapeStatus, Source, ValidatedURL
from aizk.extractors.base import ExtractionError, Extractor
from aizk.extractors.chrome import ChromeSettings, detect_playwright_chromium, detect_system_chrome
from aizk.extractors.utils import bin_version
from aizk.utilities.file_helpers import AtomicWriter
from aizk.utilities.path_helpers import (
    DEFAULT_ENV_PATH,
    ExecPath,
    SysPATH,
    add_node_bindir_to_syspath,
    find_binary_abspath,
    get_local_bin_dir,
    path_is_dir,
    path_is_executable,
    path_is_file,
    symlink_to_bin,
)

logger = logging.getLogger(__name__)


class SingleFileSettings(BaseSettings):
    """Default SingleFile Settings."""

    model_config = SettingsConfigDict(extra="ignore")

    binary: str = Field(default=str(find_binary_abspath("single-file", add_node_bindir_to_syspath())))
    timeout: int = Field(default=45, ge=15, lt=3600)

    singlefile_args: List[str] = Field(
        default=[
            "--dump-content",  # Dump the content of the processed page in the console ('true' when running in Docker) <boolean>
        ]
    )


class SingleFileExtractor(Extractor):
    """single-file extractor."""

    name: str = "single-file"
    default_filename: str = "content.html"
    config: SingleFileSettings

    def __init__(
        self,
        config: SingleFileSettings | dict[str, Any] | None = None,
        chrome_config: ChromeSettings | dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
        ensure_out_dir: bool = False,
    ):
        config = self.validate_config(config or {})

        binary = config.binary or find_binary_abspath(self.name, add_node_bindir_to_syspath())

        super().__init__(
            config=config,
            binary=binary,
            out_dir=out_dir or Path.cwd() / "data" / self.name,
            ensure_out_dir=ensure_out_dir,
        )

        self.chrome_config = ChromeSettings.model_validate(chrome_config or {})

    @override
    def validate_config(self, cfg: SingleFileSettings | dict[str, Any]) -> SingleFileSettings:
        """Validate the extractor config."""
        return SingleFileSettings.model_validate(cfg)

    @override
    def cleanup(self):
        """Clean up any state or runtime files that Chrome leaves behind when killed by a timeout or other error."""
        try:
            linux_lock_file = Path("~/.config/chromium/SingletonLock").expanduser()
            linux_lock_file.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"Ignoring exception {e}")
            pass

        if self.chrome_config.chrome_profile_dir:
            try:
                (self.chrome_config.chrome_profile_dir / "SingletonLock").unlink(missing_ok=True)
            except Exception as e:
                logger.debug(f"Ignoring exception {e}")
                pass

    def cmd(self, url: ValidatedURL | str) -> List[str]:
        """Generate CLI command."""
        chrome_args = [f"--browser-arg={option}" for option in self.chrome_config.chrome_args]

        singlefile_args = [
            f"--browser-executable-path={self.chrome_config.binary}",
            "--dump-content",
        ]

        cmd = [
            str(self.binary),
            url,
            *chrome_args,
            *singlefile_args,
        ]

        return cmd

    @override
    async def run(self, url: ValidatedURL | str, out_dir: Path):
        """Run the extraction."""
        cmd = self.cmd(url)
        logger.debug(f"Running single-file extraction with cli {cmd=}")
        result = subprocess.run(  # NOQA: S603
            cmd,  # NOQA: S607
            cwd=out_dir,
            capture_output=True,
            text=True,
            timeout=self.config.timeout,
        )

        try:
            result.check_returncode()  # raises error if failed
        except subprocess.CalledProcessError as e:
            self.cleanup()
            raise ExtractionError(f"{self.name} extraction of {url} failed:\n'{result.stderr}'") from e

        return result.stdout
