import datetime
from hashlib import file_digest
import json
import logging
import os
from pathlib import Path
from typing import Any, Tuple, override
from urllib.parse import quote, unquote, urlparse

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import requests
from tqdm.auto import tqdm

from aizk.datamodel.schema import ScrapeStatus, Source
from aizk.extractors.utils import atomic_write, download_file, validate_file
from aizk.utilities.path_helpers import (
    HostBinPath,
    PATHStr,
    add_node_bin_to_PATH,
    find_binary_abspath,
    path_is_abspath,
    path_is_dir,
    path_is_file,
)

logger = logging.getLogger(__name__)

STATICFILE_EXTENSIONS = {
    # 99.999% of the time, URLs ending in these extensions are static files,
    # and can be downloaded as-is, not html pages that need to be rendered
    "gif", "jpeg", "jpg", "png", "tif", "tiff", "wbmp", "ico", "jng", "bmp",
    "svg", "svgz", "webp", "ps", "eps", "ai", "mp3", "mp4", "m4a", "mpeg",
    "mpg", "mkv", "mov", "webm", "m4v", "flv", "wmv", "avi", "ogg", "ts", "m3u8",
    "pdf", "txt", "rtf", "rtfd", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
    "atom", "rss", "css", "js", "json", "dmg", "iso", "img", "rar", "war",
    "hqx", "zip", "gz", "bz2", "7z",
    # Less common extensions to consider adding later
    # jar, swf, bin, com, exe, dll, deb
    # ear, hqx, eot, wmlc, kml, kmz, cco, jardiff, jnlp, run, msi, msp, msm,
    # pl pm, prc pdb, rar, rpm, sea, sit, tcl tk, der, pem, crt, xpi, xspf,
    # ra, mng, asx, asf, 3gpp, 3gp, mid, midi, kar, jad, wml, htc, mml
    # These are always treated as pages, not as static files, never add them:
    # html, htm, shtml, xhtml, xml, aspx, php, cgi
}  # fmt: skip


class ExtractionError(Exception):
    """Error occurred during extraction process."""


class ExtractorSettings(BaseSettings):
    """Default configuration."""

    timeout: int = Field(default=45, ge=15, lt=3600)
    model_config = SettingsConfigDict(extra="ignore")


class Extractor:
    """Base class for extractors."""

    name: str = ""
    default_filename: str = ""

    def __init__(
        self,
        # binary: Path | str,
        out_dir: Path | str | None = None,
        config: BaseSettings | dict[str, Any] | None = None,
    ):
        # self.binary = binary  # calls setter
        self.out_dir = out_dir  # calls setter
        self.config = config  # calls setter

    @property
    def binary(self) -> Path | str:  # NOQA:D102
        return self._binary

    def init_binary(self, bin_path_or_name: Path | str, syspath: PATHStr | None = None) -> HostBinPath | Path:
        """Run any system setup required to initialize binary."""
        if bin_path_or_name is None:  # e.g. type or range check
            raise ValueError("'binary' must be provided as Path or name")

        return find_binary_abspath(bin_path_or_name, syspath)

    @binary.setter
    def binary(self, bin_path_or_name: Path | str, syspath: PATHStr | None = None):
        self._binary = self.init_binary(bin_path_or_name, syspath)

    @property
    def config(self):  # NOQA:D102
        return self._config

    def validate_config(self, config: BaseSettings | dict[str, Any]):
        """Validate the extractor config."""
        return config

    @config.setter
    def config(self, config: BaseSettings | dict[str, Any] | None):
        self._config = self.validate_config(config or ExtractorSettings())

    @property
    def out_dir(self) -> Path:  # NOQA:D102
        return self._out_dir

    @out_dir.setter
    def out_dir(self, out_dir: Path | str | None):
        self._out_dir = path_is_dir(Path(out_dir) if out_dir else Path(".") / "data")

    def cleanup(self):
        """Clean up state if needed."""
        pass

    def run(self, url: str):
        """Run the extraction."""
        raise NotImplementedError

    def transform_extract(self, extract: Any) -> str:
        """Validate the extraction."""
        return extract

    def validate_extract(self, extract: str) -> bool:
        """Validate the extraction."""
        if isinstance(extract, str) and len(extract) > 0:
            return True
        else:
            # return ScrapeStatus.ERROR
            raise ExtractionError("Extract failed validation.")

    @classmethod
    def save(cls, extract: Any, file_path: Path):
        """Save the extract to file."""
        with atomic_write(file_path) as f:
            f.write(extract)

    @classmethod
    def validate_file(cls, file_path: Path, min_bytes: int = 1) -> bool:
        """Validate that downloaded file exists and is of minimum size."""
        if validate_file(file_path, min_bytes):
            return True
        else:
            raise ExtractionError(f"File does not exist in expected download location or is empty: {file_path}")

    @classmethod
    def hash(cls, file_path: Path) -> str:
        """Hash file contents."""
        file_path = path_is_file(file_path)
        with file_path.open("rb", buffering=0) as f:
            content_hash = file_digest(f, "sha256").hexdigest()  # req. py>=3.11
        return content_hash

    def __call__(self, source: Source) -> Source:
        """Execute extraction pipeline."""
        src = source.model_copy()

        out_dir_path = self.out_dir / str(src.uuid)
        out_dir_path.mkdir(exist_ok=True)
        src.scraped_at = datetime.datetime.now(datetime.timezone.utc)

        try:
            out_file_path = out_dir_path / self.default_filename
            extract = self.run(src.url)
            extract = self.transform_extract(extract)

            if self.validate_extract(extract):
                self.save(extract, out_file_path)

            if self.validate_file(out_file_path):
                status = ScrapeStatus.COMPLETE

            src.scrape_status = status
            src.content_hash = self.hash(out_file_path)
            src.file = str(out_file_path)

        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_path / "errors.txt").open("a") as f:
                lines = [str(src.scraped_at), f"Failed to extract url {src.url}", f"Error: {str(e)}"]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()

        return src


class StaticFileExtractor(Extractor):
    """Static file extractor (i.e., pdf, txt, ...)."""

    name: str = "static"
    default_filename: str = ""

    def __init__(
        self,
        config: ExtractorSettings | dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
    ):
        self.binary = find_binary_abspath("curl", add_node_bin_to_PATH())

        super().__init__(
            config=config or ExtractorSettings(),
            out_dir=out_dir or Path.cwd() / "data" / self.name,
        )

        # self.config = self.validate_config(config or PostlightSettings())
        # self.out_dir = out_dir or Path.cwd() / "data" / PostlightExtractor.name

    @override
    def run(self, url: str, file_path: Path):
        """Run the extraction."""
        download_file(url, file_path, timeout=self.config.timeout)

    @override
    def __call__(self, source: Source) -> Source:
        """Execute extraction pipeline."""
        src = source.model_copy()

        out_dir_path = self.out_dir / str(src.uuid)
        out_dir_path.mkdir(exist_ok=True)
        src.scraped_at = datetime.datetime.now(datetime.timezone.utc)

        try:
            pagename = urlparse(src.url).path.rsplit("/", 1)[-1]
            out_file_path = out_dir_path / pagename

            # this is different from standard (no extract text to validate and save)
            self.run(src.url, out_file_path)
            if self.validate_file(out_file_path):
                status = ScrapeStatus.COMPLETE

            src.scrape_status = status
            src.content_hash = self.hash(out_file_path)
            src.file = str(out_file_path)

        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_path / "errors.txt").open("a") as f:
                lines = [str(src.scraped_at), f"Failed to extract url {src.url}", f"Error: {str(e)}"]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()

        return src
