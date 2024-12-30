import asyncio
import datetime
from hashlib import file_digest
import json
import logging
import os
from pathlib import Path
from typing import Any, Tuple, override
from urllib.parse import urlparse

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from tqdm.auto import tqdm

from aizk.datamodel.schema import ScrapeStatus, Source
from aizk.extractors.utils import download_file, get_write_mode, validate_file
from aizk.utilities.file_helpers import AtomicWriter
from aizk.utilities.parse import detect_encoding
from aizk.utilities.path_helpers import path_is_dir, path_is_file, path_is_valid

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
        config: BaseSettings | dict[str, Any] | None = None,
        binary: Path | str | None = None,
        out_dir: Path | str | None = None,
        ensure_out_dir: bool = False,
    ):
        self.config = self.validate_config(config or {})  # calls setter

        self.binary = binary or "not specified"  # calls setter

        out_dir = out_dir or Path.cwd().expanduser() / "data"
        if ensure_out_dir:
            self._ensure_out_dir(out_dir)

        self.out_dir = out_dir  # calls setter

    @property
    def config(self):  # NOQA:D102
        return self._config

    @config.setter
    def config(self, config: BaseSettings):
        self._config = config

    def validate_config(self, cfg: BaseSettings | dict[str, Any]):
        """Validate the extractor config."""
        return ExtractorSettings.model_validate(cfg)

    @property
    def binary(self) -> Path | str:  # NOQA:D102
        return self._binary

    @binary.setter
    def binary(self, binary: Path | str):
        self._binary = binary

    def _ensure_out_dir(self, out_dir):
        """Ensure save location exists."""
        p = path_is_valid(out_dir)
        if not p.is_dir():
            logging.info(f"Creating save location {p}")
            p.mkdir(parents=True, exist_ok=True)

    @property
    def out_dir(self) -> Path:  # NOQA:D102
        return self._out_dir

    @out_dir.setter
    def out_dir(self, out_dir: Path | str):
        self._out_dir = path_is_dir(out_dir)

    def cleanup(self):
        """Clean up state if needed."""
        pass

    async def run(self, url: str, out_dir: Path):
        """Run the extraction."""
        raise NotImplementedError

    def transform_extract(self, extract: str | bytes) -> str:
        """Transform the extraction."""
        if isinstance(extract, bytes):
            encoding = detect_encoding(extract)
            return extract.decode(encoding=encoding)
        elif isinstance(extract, str):
            return extract
        else:
            raise TypeError("Unexpected extraction type not str or bytes")

    def validate_extract(self, extract: str) -> bool:
        """Validate the extraction."""
        if isinstance(extract, (str)) and len(extract) > 0:
            return True
        else:
            raise ExtractionError("Extract failed validation.")

    @classmethod
    def save(cls, extract: Any, file_path: Path):
        """Save the extract to file."""
        with AtomicWriter(file_path, binary_mode=get_write_mode(extract) == "wb") as f:
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

    async def __call__(self, source: Source) -> Source:
        """Execute extraction pipeline."""
        src = source.model_copy()

        out_dir_uuid = self.out_dir / str(src.uuid)
        out_dir_uuid.mkdir(exist_ok=True)

        src.scraped_at = datetime.datetime.now(datetime.timezone.utc)

        try:
            logger.info(f"Extracting from {src.url} with Extractor")
            extract = await self.run(src.url, out_dir_uuid)
        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_uuid / "errors.txt").open("a") as f:
                lines = [
                    str(src.scraped_at),
                    f"Failed to extract url {src.url}",
                    f"Error: {str(e)}",
                ]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()
            return src

        try:
            logger.debug("Validating extraction...")
            extract = self.transform_extract(extract)
            if self.validate_extract(extract):
                logger.debug("Extraction validation successful!")
        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_uuid / "errors.txt").open("a") as f:
                lines = [
                    str(src.scraped_at),
                    f"Failed to validate extraction from url {src.url}",
                    f"Error: {str(e)}",
                ]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()
            return src

        try:
            out_file_path = out_dir_uuid / self.default_filename

            logger.debug(f"Saving to file {str(out_file_path)}...")
            self.save(extract, out_file_path)

            logger.debug("Validating savefile...")
            if self.validate_file(out_file_path):
                logger.debug("Savefile validation successful!")

        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_uuid / "errors.txt").open("a") as f:
                lines = [
                    str(src.scraped_at),
                    f"Failed to extract url {src.url}",
                    f"Error: {str(e)}",
                ]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()
            return src

        src.scrape_status = ScrapeStatus.COMPLETE
        src.content_hash = self.hash(out_file_path)
        src.file = str(out_file_path)
        return src


class StaticFileExtractor(Extractor):
    """Static file extractor (i.e., pdf, txt, ...)."""

    name: str = "staticfile"
    default_filename: str = ""

    def __init__(
        self,
        config: ExtractorSettings | dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
        ensure_out_dir: bool = False,
    ):
        super().__init__(
            config=config or ExtractorSettings(),
            out_dir=out_dir or Path.cwd() / "data" / self.name,
            ensure_out_dir=ensure_out_dir,
        )

    @override
    async def run(self, url: str, out_dir: Path, filename: str):
        """Run the extraction."""
        download_file(url, out_dir / filename, timeout=self.config.timeout)

    @override
    async def __call__(self, source: Source) -> Source:
        """Execute extraction pipeline."""
        src = source.model_copy()

        out_dir_uuid = self.out_dir / str(src.uuid)
        out_dir_uuid.mkdir(exist_ok=True)
        src.scraped_at = datetime.datetime.now(datetime.timezone.utc)

        try:
            pagename = urlparse(src.url).path.rsplit("/", 1)[-1]  # rightmost part of the path

            # this is different from standard (no extract text to validate and save)
            await self.run(src.url, out_dir=out_dir_uuid, filename=pagename)
            if self.validate_file(out_dir_uuid / pagename):
                status = ScrapeStatus.COMPLETE

            src.scrape_status = status
            src.content_hash = self.hash(out_dir_uuid / pagename)
            src.file = str(out_dir_uuid / pagename)

        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_uuid / "errors.txt").open("a") as f:
                lines = [str(src.scraped_at), f"Failed to extract url {src.url}", f"Error: {str(e)}"]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()

        return src
