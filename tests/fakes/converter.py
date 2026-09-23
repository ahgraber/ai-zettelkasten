"""A converter that returns the fixture article's Markdown."""

from __future__ import annotations

from typing import Any, ClassVar

from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput
from tests.fakes import FIXTURES_DIR

ARTICLE_TITLE = "The Analytical Engine and Its Programmer"


class FixturedDoclingConverter:
    """A Converter that records every input it receives and returns the fixture article's Markdown."""

    supported_formats: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML, ContentType.PDF})
    requires_gpu: ClassVar[bool] = False

    def __init__(self) -> None:
        """Start with no recorded inputs."""
        self.inputs: list[ConversionInput] = []

    def convert(self, input: ConversionInput) -> ConversionArtifacts:  # noqa: A002
        """Record ``input`` and return the fixture Markdown under a fixed docling version."""
        self.inputs.append(input)
        return ConversionArtifacts(
            markdown=(FIXTURES_DIR / "article.md").read_text(encoding="utf-8"),
            metadata={"docling_version": "fixture"},
            document_title=ARTICLE_TITLE,
        )

    def config_snapshot(self) -> dict[str, Any]:
        """Return the fixed output-affecting configuration."""
        return {"converter": "fixture", "version": 1}
