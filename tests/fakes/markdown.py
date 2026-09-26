"""A deterministic stand-in for the graph's Markdown source."""

from __future__ import annotations

from aizk.contextualization.processing.document import LoadedMarkdown
from aizk.utilities.hashing import compute_markdown_hash


class StubMarkdownSource:
    """A deterministic Markdown source returning fixed text with its true hash."""

    def __init__(self, text: str = "# doc") -> None:
        """Store the Markdown returned for every locator."""
        self.text = text

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        """Return the stub Markdown and its computed hash for any locator."""
        return LoadedMarkdown(text=self.text, markdown_hash_xx64=compute_markdown_hash(self.text))
