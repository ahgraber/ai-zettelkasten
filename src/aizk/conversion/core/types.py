"""Shared types for the conversion core: ContentType, ConversionInput, ConversionArtifacts."""

from __future__ import annotations

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


class ConversionInput(BaseModel):
    """Fetched source bytes with their authoritative content type."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    content_type: ContentType
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversionArtifacts(BaseModel):
    """Converter output. Exact shape for figures/metadata firms up in later PRs."""

    model_config = ConfigDict(frozen=True)

    markdown: str
    # figures and metadata are intentionally permissive at this stage;
    # concrete shape will be locked down when the Docling adapter lands.
    figures: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
