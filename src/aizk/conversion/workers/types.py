"""Shared data types for the conversion worker."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ConversionInput:
    """Source bytes and processing pipeline information."""

    pipeline: Literal["html", "pdf"]
    content_bytes: bytes
    fetched_at: dt.datetime


@dataclass(frozen=True)
class ConversionArtifacts:
    """Local conversion artifacts generated in phase one."""

    markdown_path: Path
    figure_paths: list[Path]
    markdown_hash: str
    pipeline_name: str
    fetched_at: dt.datetime
    docling_version: str


@dataclass(frozen=True, slots=True)
class SupervisionResult:
    """Return values for conversion subprocess supervision."""

    last_phase: str
    reported_error: dict[str, str] | None
    cancelled: bool
    timed_out: bool


def _utcnow() -> dt.datetime:
    """Return timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)
