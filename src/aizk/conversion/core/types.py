"""Shared types for the conversion core: ContentType, ConversionInput, ConversionArtifacts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field


class ContentType(str, Enum):
    """Closed enumeration of content types the pipeline can handle."""

    PDF = "pdf"
    HTML = "html"
    IMAGE = "image"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    CSV = "csv"


@dataclass(frozen=True)
class SourceMetadata:
    """Descriptive metadata about a source document, separate from its fetch identity.

    Flows alongside the SourceRef through the resolver/fetcher chain and is merged
    at each hop using field-wise "earlier non-None wins" semantics: the resolver sees
    the most authoritative source identity, and downstream stages fill in fields the
    resolver could not observe — they do not override it.
    """

    source_url: str | None = None
    normalized_url: str | None = None
    document_base_url: str | None = None
    resolver_title: str | None = None

    def merge(self, other: SourceMetadata) -> SourceMetadata:
        """Return a new SourceMetadata where self's non-None fields take precedence over other's."""
        return SourceMetadata(
            source_url=self.source_url if self.source_url is not None else other.source_url,
            normalized_url=self.normalized_url if self.normalized_url is not None else other.normalized_url,
            document_base_url=self.document_base_url
            if self.document_base_url is not None
            else other.document_base_url,
            resolver_title=self.resolver_title if self.resolver_title is not None else other.resolver_title,
        )


class ConversionInput(BaseModel):
    """Fetched source bytes with their authoritative content type and source metadata."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    content: bytes
    content_type: ContentType
    source_meta: SourceMetadata = Field(default_factory=SourceMetadata)


class ConversionArtifacts(BaseModel):
    """Converter output: markdown, figures, and optional document title."""

    model_config = ConfigDict(frozen=True)

    markdown: str
    figures: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    document_title: str | None = None


# Canonical mapping from SourceRef.kind literals to the source_type classification
# stored on the Source row. source_type is the resolved semantic origin (used for
# UI/filtering), distinct from the ingress ref shape (source_ref.kind).
SOURCE_TYPE_BY_KIND: Mapping[str, str] = {
    "arxiv": "arxiv",
    "github_readme": "github",
    "url": "other",
    "karakeep_bookmark": "other",
    "inline_html": "other",
    "singlefile": "other",
}
