"""Based heavily on https://github.com/MarkHershey/arxiv-dl."""

import asyncio
import datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, List
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from pydantic import (
    Field,
    HttpUrl,
    ValidationError,
)
from pydantic_settings import SettingsConfigDict
import requests
from typing_extensions import override

from aizk.datamodel.schema import ScrapeStatus, Source
from aizk.extractors.base import ExtractionError, Extractor, ExtractorSettings
from aizk.extractors.utils import download_file
from aizk.utilities.file_helpers import AtomicWriter

logger = logging.getLogger(__name__)


class ArxivSettings(ExtractorSettings):
    """Default configuration."""

    timeout: int = Field(default=45, ge=15, lt=3600)
    with_html: bool = Field(default=True)
    with_metadata: bool = Field(default=True)
    with_pdf: bool = Field(default=True)

    model_config = SettingsConfigDict(extra="ignore")


class ArxivExtractor(Extractor):
    """Download file from arXiv.org."""

    name: str = "arxiv"
    # default_filename: str = ""

    def __init__(
        self,
        config: ArxivSettings | dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
        ensure_out_dir: bool = False,
    ):
        config = self.validate_config(config or {})

        super().__init__(
            config=config,
            out_dir=out_dir or Path.cwd() / "data" / self.name,
            ensure_out_dir=ensure_out_dir,
        )

    @override
    def validate_config(self, c: ArxivSettings | dict[str, Any]) -> ArxivSettings:
        """Validate the extractor config."""
        cfg = ArxivSettings.model_validate(c)
        if all(x is False for x in [cfg.with_html, cfg.with_metadata, cfg.with_pdf]):
            raise ValidationError("At least one of 'with_html', 'with_metadata', or 'with_pdf' must be True.")
        return cfg

    @classmethod
    def validate_arxiv_url(cls, url: str) -> str:
        """Validate arXiv URL."""
        if "arxiv.org" not in url:
            raise ValueError("URL must be from arXiv.org")

        try:
            _url = HttpUrl(url)
        except ValidationError:
            logger.exception(f"Invalid URL: {url}")
            raise

        if not (_url.path.startswith("/pdf") or _url.path.startswith("/abs") or _url.path.startswith("/html")):
            raise ValueError("URL must be to PDF, abstract, or HTML page")
        else:
            return str(_url)

    @classmethod
    def get_arxiv_id(cls, url: str) -> str:
        """Extract arXiv ID from url."""
        url = cls.validate_arxiv_url(url)
        path = urlparse(url).path

        # ref: https://arxiv.org/help/arxiv_identifier
        arxiv_id_regex = re.compile(r"([0-2])([0-9])(0|1)([0-9])\.[0-9]{4,5}(v[0-9]{1,2})?", re.IGNORECASE)
        match = re.search(arxiv_id_regex, path)
        if match:
            return match[0]
        else:
            raise ValueError("Could not find arXiv ID in URL.")

    @classmethod
    def to_abs_url(cls, arxiv_id: str) -> str:
        """Convert arXiv ID to abstract URL."""
        return f"https://arxiv.org/abs/{arxiv_id}"

    @classmethod
    def to_html_url(cls, arxiv_id: str) -> str:
        """Convert arXiv ID to HTML URL."""
        return f"https://arxiv.org/html/{arxiv_id}"

    @classmethod
    def to_pdf_url(cls, arxiv_id: str) -> str:
        """Convert arXiv ID to PDF URL."""
        return f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    def get_abs_metadata(self, arxiv_id: str, out_dir: Path):
        """Extract metadata from arXiv abstract page."""
        url = self.to_abs_url(arxiv_id)

        response = requests.get(url, timeout=self.config.timeout)
        if response.status_code != 200:
            raise requests.exceptions.HTTPError(f"Cannot connect to {url}")

        soup = BeautifulSoup(response.text, "html.parser")

        # title
        result = soup.find("h1", class_="title mathjax")
        tmp = [i.string for i in result]
        paper_title = tmp.pop()

        # authors
        result = soup.find("div", class_="authors")
        author_list = [i.string.strip() for i in result]
        author_list.pop(0)
        while "," in author_list:
            author_list.remove(",")

        # abstract
        result = soup.find("blockquote", class_="abstract mathjax")
        tmp = [i.string for i in result]
        paper_abstract = tmp.pop()
        tmp = paper_abstract.split("\n")
        paper_abstract = " ".join(tmp)

        metadata = {
            "title": paper_title,
            "authors": author_list,
            "abstract": paper_abstract.strip(),
        }

        with AtomicWriter(out_dir / "metadata.json", binary_mode=False) as f:
            json.dump(metadata, f)

    def get_html_content(self, arxiv_id: str, out_dir: Path):
        """Extract HTML content from arXiv HTML page."""
        url = self.to_html_url(arxiv_id)
        try:
            download_file(url, out_dir / f"{arxiv_id}.html", timeout=self.config.timeout)
        except requests.exceptions.HTTPError:
            # Log but don't raise if HTML page is not available
            logger.warning(f"Could not download HTML content from {url}, check if page exists.")

    def get_pdf_file(self, arxiv_id: str, out_dir: Path):
        """Download PDF file from arXiv."""
        url = self.to_pdf_url(arxiv_id)
        download_file(url, out_dir / f"{arxiv_id}.pdf", timeout=self.config.timeout)

    @override
    async def run(self, url: str, out_dir: Path):
        """Run the extraction."""
        arxiv_id = self.get_arxiv_id(url)

        # these functions download the files
        if self.config.with_metadata:
            self.get_abs_metadata(arxiv_id, out_dir)

        if self.config.with_pdf:
            self.get_pdf_file(arxiv_id, out_dir)

        if self.config.with_html:
            self.get_html_content(arxiv_id, out_dir)

        return out_dir

    @override
    async def __call__(self, source: Source) -> Source:
        """Execute extraction pipeline."""
        src = source.model_copy()

        out_dir_uuid = self.out_dir / str(src.uuid)
        out_dir_uuid.mkdir(exist_ok=True)
        src.scraped_at = datetime.datetime.now(datetime.timezone.utc)
        arxiv_id = self.get_arxiv_id(src.url)

        try:
            logger.info(f"Extracting from {src.url} with ArxivExtractor")
            await self.run(src.url, out_dir_uuid)

            file_validations = []
            if self.config.with_metadata:
                file_validations.append(self.validate_file(out_dir_uuid / "metadata.json"))
            if self.config.with_pdf:
                file_validations.append(self.validate_file(out_dir_uuid / f"{arxiv_id}.pdf"))
            if self.config.with_html and (out_dir_uuid / f"{arxiv_id}.html").exists():
                # if file doesn't exist, then html page may not exist (which is sometimes expected)
                file_validations.append(self.validate_file(out_dir_uuid / f"{arxiv_id}.html"))

            if all(file_validations):
                src.scrape_status = ScrapeStatus.COMPLETE
            else:
                raise ExtractionError("Not all files were downloaded successfully")  # NOQA: TRY301

            priority_file = (
                out_dir_uuid / f"{arxiv_id}.pdf"
                if (out_dir_uuid / f"{arxiv_id}.pdf").exists()
                else (out_dir_uuid / f"{arxiv_id}.html")
                if (out_dir_uuid / f"{arxiv_id}.html").exists()
                else out_dir_uuid / "metadata.json"
            )
            src.content_hash = self.hash(priority_file)
            src.file = str(priority_file)

        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_uuid / "errors.txt").open("a") as f:
                lines = [str(src.scraped_at), f"Failed to extract url {src.url}", f"Error: {str(e)}"]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()

        return src
