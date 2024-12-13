import datetime
import json
import logging
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, run
from typing import Any, Tuple, override

from ai_zk.datamodel.schema import ScrapeStatus, Source, ValidatedURL
from ai_zk.extractors.base import ExtractionError, Extractor
from ai_zk.extractors.utils import atomic_write
from ai_zk.utilities.path_helpers import add_node_bin_to_PATH, find_binary_abspath
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class PostlightSettings(BaseSettings):
    """Configuration for @postlight/parser."""

    timeout: int = Field(default=45, ge=15, lt=3600)
    model_config = SettingsConfigDict(extra="ignore")


class PostlightExtractor(Extractor):
    """@postlight/parser extractor."""

    name: str = "postlight-parser"
    default_filename: str = "content.html"
    config: PostlightSettings

    def __init__(
        self,
        config: PostlightSettings | dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
    ):
        self.binary = find_binary_abspath(self.name, add_node_bin_to_PATH())

        super().__init__(
            config=config or PostlightSettings(),
            out_dir=out_dir or Path.cwd() / "data" / PostlightExtractor.name,
        )

        # self.config = self.validate_config(config or PostlightSettings())
        # self.out_dir = out_dir or Path.cwd() / "data" / PostlightExtractor.name

    @override
    def validate_config(self, cfg: PostlightSettings | dict[str, Any]) -> PostlightSettings:
        """Validate the extractor config."""
        return PostlightSettings.model_validate(cfg)

    @override
    def run(self, url: ValidatedURL | str):
        """Run the extraction."""
        # Get HTML version of article
        cmd = [str(self.binary), url]
        logger.debug(f"{cmd=}")
        result = run(  # NOQA: S603
            cmd,  # NOQA: S603
            capture_output=True,
            timeout=self.config.timeout,
        )

        try:
            result.check_returncode()  # raises error if failed
        except CalledProcessError as e:
            raise ExtractionError(f"{self.name} extraction of {url} failed:\n'{result.stderr.decode()}'") from e

        return result.stdout

    @override
    def validate_extract(self, extract: str) -> ScrapeStatus:
        try:
            article_json = json.loads(extract)
        except json.JSONDecodeError:
            return ScrapeStatus.ERROR

        if article_json.get("error") or article_json.get("failed") or (article_json.get("content") is None):
            return ScrapeStatus.ERROR

        return ScrapeStatus.COMPLETE

    @override
    def save(self, extract: Any, file_path: Path):
        """Save the extract to file."""
        article_json = json.loads(extract)
        content = article_json.pop("content")

        with atomic_write(file_path) as f:
            json.dump(content, f)

        out_dir_path = file_path.parent
        with atomic_write(out_dir_path / "metadata.json") as f:
            json.dump(article_json, f)

    @override
    def __call__(self, source: Source): ...
