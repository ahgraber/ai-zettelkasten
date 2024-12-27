"""PostlightExtractor.

@postlight/parser formerly known as @postlight/mercury-parser
ref: https://github.com/ArchiveBox/ArchiveBox/blob/dev/archivebox/pkgs/abx-plugin-mercury/abx_plugin_mercury/mercury.py
"""

import datetime
import json
import logging
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, run
from typing import Any, List, Tuple, override

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from aizk.datamodel.schema import ScrapeStatus, Source, ValidatedURL
from aizk.extractors.base import ExtractionError, Extractor
from aizk.extractors.utils import atomic_write
from aizk.utilities.path_helpers import add_node_bindir_to_syspath, find_binary_abspath

logger = logging.getLogger(__name__)


class PostlightSettings(BaseSettings):
    """Configuration for @postlight/parser."""

    binary: str = Field(default=str(find_binary_abspath("postlight-parser", add_node_bindir_to_syspath())))
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
        config = self.validate_config(config or {})
        binary = config.binary or find_binary_abspath(self.name, add_node_bindir_to_syspath())

        super().__init__(
            config=config,
            binary=binary,
            out_dir=out_dir or Path.cwd() / "data" / self.name,
        )

    @override
    def validate_config(self, cfg: PostlightSettings | dict[str, Any]) -> PostlightSettings:
        """Validate the extractor config."""
        return PostlightSettings.model_validate(cfg)

    def cmd(self, url: ValidatedURL | str) -> List[str]:
        """Generate CLI command."""
        cmd = [
            str(self.binary),
            url,
        ]
        return cmd

    @override
    def run(self, url: ValidatedURL | str, out_dir: Path):
        """Run the extraction."""
        # Get HTML version of article
        cmd = self.cmd(url)
        logger.debug(f"{cmd=}")
        result = run(  # NOQA: S603
            cmd,  # NOQA: S607
            cwd=out_dir,
            capture_output=True,
            text=True,
            timeout=self.config.timeout,
        )

        try:
            result.check_returncode()  # raises error if failed
        except CalledProcessError as e:
            self.cleanup()
            raise ExtractionError(f"{self.name} extraction of {url} failed:\n'{result.stderr}'") from e

        return result.stdout

    @override
    def validate_extract(self, extract: str) -> bool:
        try:
            article_json = json.loads(extract)
        except json.JSONDecodeError as e:
            raise ExtractionError("Extract failed validation.") from e

        return article_json.get("error") or article_json.get("failed") or (article_json.get("content") is None)

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

    # @override
    # def __call__(self, source: Source): ...
