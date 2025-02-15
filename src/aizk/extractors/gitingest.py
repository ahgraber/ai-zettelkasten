"""Relies on https://github.com/cyclotruc/gitingest/."""

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
from gitingest import ingest
from pydantic import (
    Field,
    HttpUrl,
    ValidationError,
)
from pydantic_settings import SettingsConfigDict
from typing_extensions import override

import requests

from aizk.datamodel.schema import ScrapeStatus, Source, ValidatedURL
from aizk.extractors.base import ExtractionError, Extractor, ExtractorSettings
from aizk.extractors.utils import download_file
from aizk.utilities.file_helpers import AtomicWriter
from aizk.utilities.log_helpers import suppress_logs

logger = logging.getLogger(__name__)


class GitHubExtractor(Extractor):
    """GitHub extractor."""

    default_filename: str = "repo.md"

    def __init__(
        self,
        config: ExtractorSettings | dict[str, Any] | None = None,
        data_dir: Path | str | None = None,
        ensure_data_dir: bool = False,
    ):
        super().__init__(
            config=config or ExtractorSettings(),
            data_dir=data_dir,
            ensure_data_dir=ensure_data_dir,
        )

    @override
    async def run(self, url: ValidatedURL | str):
        """Run the extraction."""
        summary, tree, content = ingest(url)
        return "\n\n".join([tree, content])
