"""Shared value types for the conversion pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ContentType(str, Enum):
    pdf = "pdf"
    html = "html"
    image = "image"
    docx = "docx"
    pptx = "pptx"
    xlsx = "xlsx"
    csv = "csv"


@dataclass(frozen=True)
class ConversionInput:
    """Fetched bytes ready for conversion."""

    content: bytes
    content_type: ContentType
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversionArtifacts:
    """Output produced by a converter."""

    markdown: str
    figures: list[bytes] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
