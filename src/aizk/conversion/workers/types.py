"""Shared data types for the conversion worker."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from aizk.conversion.core.types import SourceMetadata


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
    shutdown_terminated: bool = False


class _SourceMetaFields(BaseModel):
    """Serializable representation of SourceMetadata for IPC across the process boundary."""

    model_config = ConfigDict(extra="forbid")

    source_url: str | None = None
    normalized_url: str | None = None
    document_base_url: str | None = None
    resolver_title: str | None = None

    def to_source_metadata(self) -> SourceMetadata:
        """Convert to the in-process SourceMetadata dataclass."""
        from aizk.conversion.core.types import SourceMetadata

        return SourceMetadata(
            source_url=self.source_url,
            normalized_url=self.normalized_url,
            document_base_url=self.document_base_url,
            resolver_title=self.resolver_title,
        )

    @classmethod
    def from_source_metadata(cls, meta: SourceMetadata) -> _SourceMetaFields:
        """Construct from the in-process SourceMetadata dataclass."""
        return cls(
            source_url=meta.source_url,
            normalized_url=meta.normalized_url,
            document_base_url=meta.document_base_url,
            resolver_title=meta.resolver_title,
        )


class SubprocessMetadata(BaseModel):
    """Typed IPC schema for conversion subprocess results written to metadata.json.

    Both the subprocess (writer) and the parent (reader) use this model with
    ``extra="forbid"`` so schema drift fails loudly rather than silently
    dropping or carrying unknown fields.
    """

    model_config = ConfigDict(extra="forbid")

    pipeline_name: Literal["html", "pdf"]
    terminal_ref: dict[str, Any]
    content_type: str
    markdown_filename: str
    figure_files: list[str]
    markdown_hash_xx64: str
    docling_version: str
    config_snapshot: dict[str, Any]
    fetched_at: str
    source_meta: _SourceMetaFields
    document_title: str | None
    source_title: str | None


_UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$|^[0-9a-f\-]{32,36}$", re.IGNORECASE)


def select_source_title(document_title: str | None, resolver_title: str | None) -> str | None:
    """Select the best human-readable title from converter and resolver candidates.

    Preference order:
    1. ``document_title`` when non-empty, not UUID-shaped, and not a bare URL.
    2. ``resolver_title`` when non-empty.
    3. ``None`` when neither candidate qualifies.

    UUID-shaped strings (32-36 hex/dash characters) and strings starting with
    ``http://`` or ``https://`` are rejected as low-confidence Docling extractions.
    """

    def _is_usable(title: str | None) -> bool:
        if not title or not title.strip():
            return False
        t = title.strip()
        if t.lower().startswith("http://") or t.lower().startswith("https://"):
            return False
        return not _UUID_RE.match(t)

    if _is_usable(document_title):
        return document_title.strip()  # type: ignore[union-attr]
    if _is_usable(resolver_title):
        return resolver_title.strip()  # type: ignore[union-attr]
    return None


def _utcnow() -> dt.datetime:
    """Return timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)
