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
from aizk.extractors.utils import atomic_write
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

    @classmethod
    def is_static_file(cls, url: str):
        """Determine whether file is static or requires rendering."""
        # TODO: the proper way is with MIME type detection + ext, not only extension
        pagename = urlparse(url).path.rsplit("/", 1)[-1]
        extension = Path(pagename).suffix.replace(".", "")
        return extension.lower() in cls.STATICFILE_EXTENSIONS

    @classmethod
    def download_file(cls, url: str, file_path: Path, timeout: int = 600):
        """Download a file."""
        try:
            # First, send a HEAD request to get file size
            head_response = requests.head(url, timeout=timeout)
            head_response.raise_for_status()
            total_size = int(head_response.headers.get("content-length", 0))
        except requests.exceptions.RequestException:
            logger.exception("Could not query file head")

        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                # Raise an exception for bad status codes
                response.raise_for_status()

                # Get the total file size
                total_size = int(response.headers.get("content-length", 0))

                with (
                    atomic_write(file_path, is_binary=True) as f,
                    tqdm(total=total_size, unit="iB", unit_scale=True, desc=file_path.name) as progress_bar,
                ):
                    for chunk in response.iter_content(chunk_size=8192):
                        size = f.write(chunk)
                        progress_bar.update(size)

            logger.info(f"File downloaded successfully: {file_path}")

        except requests.exceptions.RequestException:
            logger.exception("Download failed")

    @classmethod
    def validate_download(cls, file_path: Path, min_bytes: int = 1) -> ScrapeStatus:
        """Validate that downloaded file exists and is of minimum size."""
        file_path = path_is_file(file_path)
        if (file_size := file_path.stat().st_size) >= min_bytes:
            return ScrapeStatus.COMPLETE
        else:
            raise ExtractionError(f"File is too small: found {file_size} bytes, expected at least {min_bytes} bytes.")
            # return ScrapeStatus.ERROR

    def cleanup(self):
        """Clean up state if needed."""
        pass

    def run(self, url: str):
        """Run the extraction."""
        raise NotImplementedError

    def transform_extract(self, extract: Any) -> str:
        """Validate the extraction."""
        return extract

    def validate_extract(self, extract: str) -> ScrapeStatus:
        """Validate the extraction."""
        if isinstance(extract, str) and len(extract) > 0:
            return ScrapeStatus.COMPLETE
        else:
            # return ScrapeStatus.ERROR
            raise ExtractionError("Extract failed validation.")

    @classmethod
    def save(cls, extract: Any, file_path: Path):
        """Save the extract to file."""
        with atomic_write(file_path) as f:
            f.write(extract)

    @classmethod
    def hash(cls, file_path: Path) -> str:
        """Hash file contents."""
        file_path = path_is_file(file_path)
        with file_path.open("rb", buffering=0) as f:
            content_hash = file_digest(f, "sha256").hexdigest()  # req. py>=3.11
        return content_hash

    ### TODO: don't double-barrel this, just make static file extractor
    ### TODO: is_static_file can remain classmethod
    def _static_file_handler(self, src: Source, out_dir_path: Path) -> ScrapeStatus:
        """Process for static file extraction."""
        pagename = urlparse(src.url).path.rsplit("/", 1)[-1]
        out_file_path = out_dir_path / pagename
        self.download_file(src.url, out_file_path)
        return self.validate_download(out_file_path)

    def __call__(self, source: Source) -> Source:
        """Execute extraction pipeline."""
        src = source.__deepcopy__()

        out_dir_path = self.out_dir / str(src.uuid)
        out_dir_path.mkdir(exist_ok=True)
        src.scraped_at = datetime.datetime.now(datetime.timezone.utc)
        try:
            if self.is_static_file(src.url):
                self._static_file_handler(src, out_dir_path)
            else:
                out_file_path = out_dir_path / self.default_filename
                extract = self.run(src.url)
                extract = self.transform_extract(extract)
                status = self.validate_extract(extract)
                self.save(extract, out_file_path)

            src.scrape_status = status
            src.content_hash = self.hash(out_file_path)
            src.file = str(out_file_path)

        except Exception as e:
            src.scrape_status = ScrapeStatus("ERROR")
            src.error_message = str(e)

            with (out_dir_path / "errors.txt").open("a") as f:
                lines = [src.scraped_at, f"Failed to extract url {src.url}", f"Error: {str(e)}"]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()

        return src
