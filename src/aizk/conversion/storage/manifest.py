"""Manifest generation for conversion artifacts.

Two reader versions coexist:

- ``ConversionManifestV1`` reads legacy manifests written prior to the
  pluggable-fetch-convert refactor.
- ``ConversionManifestV2`` is what the writer emits today; it supports
  non-KaraKeep sources and records terminal fetch provenance explicitly.

``load_manifest`` dispatches to the right reader based on the serialized
``version`` string.  For backwards compatibility, ``ConversionManifest`` is an
alias for ``ConversionManifestV2`` so existing callers continue to work.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from aizk.conversion.datamodel.job import ConversionJob
    from aizk.conversion.datamodel.source import Source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared components
# ---------------------------------------------------------------------------


class ManifestConversionMetadata(BaseModel):
    """Conversion metadata in manifest."""

    job_id: int
    payload_version: int
    docling_version: str
    pipeline_name: Literal["html", "pdf"]
    started_at: datetime
    finished_at: datetime
    duration_seconds: int = Field(description="Duration from started_at to finished_at")


class ManifestArtifactMarkdown(BaseModel):
    """Markdown artifact metadata."""

    key: str = Field(description="Absolute S3 URI (s3://bucket/key)")
    hash_xx64: str = Field(description="xxHash64 hex digest")
    created_at: datetime


class ManifestArtifactFigure(BaseModel):
    """Figure artifact metadata."""

    key: str = Field(description="Absolute S3 URI (s3://bucket/key)")
    created_at: datetime


class ManifestArtifacts(BaseModel):
    """Artifacts section of manifest."""

    markdown: ManifestArtifactMarkdown
    figures: list[ManifestArtifactFigure]


# ---------------------------------------------------------------------------
# Provenance discriminated union (v2 only)
# ---------------------------------------------------------------------------


class ManifestProvenanceKarakeep(BaseModel):
    """Terminal fetch provenance: KaraKeep itself was the byte source."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["karakeep_bookmark"] = "karakeep_bookmark"
    bookmark_id: str


class ManifestProvenanceArxiv(BaseModel):
    """Terminal fetch provenance: arXiv PDF."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["arxiv"] = "arxiv"
    arxiv_id: str


class ManifestProvenanceGithub(BaseModel):
    """Terminal fetch provenance: GitHub README."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["github_readme"] = "github_readme"
    owner: str
    repo: str


class ManifestProvenanceUrl(BaseModel):
    """Terminal fetch provenance: direct URL fetch."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["url"] = "url"
    url: str


class ManifestProvenanceInline(BaseModel):
    """Terminal fetch provenance: inline HTML content."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["inline_html"] = "inline_html"
    content_hash: str


ManifestProvenanceVariant = Union[
    ManifestProvenanceKarakeep,
    ManifestProvenanceArxiv,
    ManifestProvenanceGithub,
    ManifestProvenanceUrl,
    ManifestProvenanceInline,
]

ManifestProvenance = Annotated[ManifestProvenanceVariant, Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# V1 manifest (legacy, read-only)
# ---------------------------------------------------------------------------


class ManifestSourceV1(BaseModel):
    """Source information in v1.0 manifest (all fields required)."""

    model_config = ConfigDict(extra="forbid")

    url: str
    normalized_url: str
    title: str
    source_type: Literal["arxiv", "github", "other"]
    fetched_at: datetime


class ManifestConfigSnapshotV1(BaseModel):
    """v1.0 Docling-specific config snapshot.

    Field set must stay in sync with build_output_config_snapshot in hashing.py.
    """

    model_config = ConfigDict(extra="forbid")

    docling_pdf_max_pages: int
    docling_enable_ocr: bool
    docling_enable_table_structure: bool
    docling_picture_description_model: str
    docling_picture_timeout: float
    docling_enable_picture_classification: bool
    picture_description_enabled: bool


class ConversionManifestV1(BaseModel):
    """Legacy v1.0 manifest reader. Not written post-refactor."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["1.0"]
    aizk_uuid: UUID
    karakeep_id: str
    source: ManifestSourceV1
    conversion: ManifestConversionMetadata
    artifacts: ManifestArtifacts
    config_snapshot: ManifestConfigSnapshotV1


# ---------------------------------------------------------------------------
# V2 manifest (current format)
# ---------------------------------------------------------------------------


class ManifestSourceV2(BaseModel):
    """Source metadata in v2 manifests. Fields nullable for non-KaraKeep sources."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    normalized_url: str | None = None
    title: str | None = None
    source_type: Literal["arxiv", "github", "other"] | None = None
    fetched_at: datetime | None = None


class ManifestConfigSnapshotV2(BaseModel):
    """v2 config snapshot: converter_name + opaque adapter-supplied fields.

    The adapter supplies its own output-affecting fields under ``adapter``;
    the manifest does not constrain their shape beyond "json-serializable".
    """

    model_config = ConfigDict(extra="forbid")

    converter_name: str = Field(description="Name of the converter that produced this output")
    adapter: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque adapter-supplied output-affecting config fields",
    )


class ConversionManifestV2(BaseModel):
    """v2 manifest with explicit provenance and nullable identity fields."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["2.0"]
    aizk_uuid: UUID
    karakeep_id: str | None = None
    provenance: ManifestProvenance = Field(
        description="Terminal fetch state — the identity that produced the converted bytes",
    )
    ingress: ManifestProvenance | None = Field(
        default=None,
        description="Submitter-supplied ref, only when it differs from the terminal provenance",
    )
    source: ManifestSourceV2
    conversion: ManifestConversionMetadata
    artifacts: ManifestArtifacts
    config_snapshot: ManifestConfigSnapshotV2


# Backwards-compatible alias so existing callers continue to work.
ConversionManifest = ConversionManifestV2
ManifestSource = ManifestSourceV2
ManifestConfigSnapshot = ManifestConfigSnapshotV2


# ---------------------------------------------------------------------------
# Version-dispatching loader
# ---------------------------------------------------------------------------


def load_manifest(data: dict) -> ConversionManifestV1 | ConversionManifestV2:
    """Load a manifest dict, dispatching to the right reader by ``version``."""
    version = data.get("version")
    if version == "1.0":
        return ConversionManifestV1.model_validate(data)
    if version == "2.0":
        return ConversionManifestV2.model_validate(data)
    raise ValueError(f"Unknown manifest version: {version!r}")


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _coerce_datetime(value: datetime | None, fallback: datetime) -> datetime:
    """Return datetime value or fallback if None."""
    chosen = value if value is not None else fallback
    if chosen.tzinfo is None:
        return chosen.replace(tzinfo=timezone.utc)
    return chosen


def _extract_arxiv_id(url: str | None) -> str | None:
    """Best-effort arxiv_id extraction from a URL; returns None if not recognizable."""
    if not url:
        return None
    try:
        from aizk.conversion.utilities.arxiv_utils import get_arxiv_id

        return get_arxiv_id(url)
    except Exception:
        return None


def _extract_github_owner_repo(url: str | None) -> tuple[str, str] | None:
    """Best-effort (owner, repo) extraction; returns None if not recognizable."""
    if not url:
        return None
    try:
        from aizk.conversion.utilities.github_utils import parse_github_owner_repo

        return parse_github_owner_repo(url)
    except Exception:
        return None


def _derive_provenance_and_ingress(
    source: Source,
) -> tuple[ManifestProvenanceVariant, ManifestProvenanceVariant | None]:
    """Compute (provenance, ingress) for a Source row.

    ``provenance`` describes the terminal fetch identity; ``ingress`` is the
    submitter-supplied ref when it differs from the terminal.

    Pre-worker-cutover bridge (TODO: remove after PR 7): the worker does not pass
    terminal state explicitly, so this function infers it from ``source.source_type``
    instead.  After PR 7 the worker will record the actual terminal ref in the
    workspace metadata and pass it directly to ``generate_manifest``.
    """
    ingress_kind = str(source.source_ref.get("kind", ""))

    # Only KaraKeep ingresses can resolve into a different terminal today.
    if ingress_kind == "karakeep_bookmark":
        if source.source_type == "arxiv":
            arxiv_id = _extract_arxiv_id(source.url)
            if not arxiv_id:
                raise ValueError(
                    f"Source {source.aizk_uuid}: source_type='arxiv' but could not"
                    f" extract arxiv_id from url={source.url!r}"
                )
            provenance = ManifestProvenanceArxiv(arxiv_id=arxiv_id)
            ingress = ManifestProvenanceKarakeep(
                bookmark_id=str(source.source_ref.get("bookmark_id", "")),
            )
            return provenance, ingress
        if source.source_type == "github":
            owner_repo = _extract_github_owner_repo(source.url)
            if owner_repo is None:
                raise ValueError(
                    f"Source {source.aizk_uuid}: source_type='github' but could not"
                    f" parse owner/repo from url={source.url!r}"
                )
            owner, repo = owner_repo
            provenance = ManifestProvenanceGithub(owner=owner, repo=repo)
            ingress = ManifestProvenanceKarakeep(
                bookmark_id=str(source.source_ref.get("bookmark_id", "")),
            )
            return provenance, ingress
        # KaraKeep-terminal cases (PDF assets, precrawled archives, text/html content)
        # fall through to provenance = karakeep_bookmark with no ingress.
        provenance = ManifestProvenanceKarakeep(
            bookmark_id=str(source.source_ref.get("bookmark_id", "")),
        )
        return provenance, None

    # Non-KaraKeep ingresses: provenance mirrors the submitted ref, no ingress.
    if ingress_kind == "arxiv":
        arxiv_id = source.source_ref.get("arxiv_id")
        if not arxiv_id:
            raise ValueError(
                f"Source {source.aizk_uuid}: arxiv source_ref missing 'arxiv_id':"
                f" {source.source_ref!r}"
            )
        return ManifestProvenanceArxiv(arxiv_id=str(arxiv_id)), None
    if ingress_kind == "github_readme":
        owner = source.source_ref.get("owner")
        repo = source.source_ref.get("repo")
        if not owner or not repo:
            raise ValueError(
                f"Source {source.aizk_uuid}: github_readme source_ref missing 'owner'"
                f" or 'repo': {source.source_ref!r}"
            )
        return (
            ManifestProvenanceGithub(owner=str(owner), repo=str(repo)),
            None,
        )
    if ingress_kind == "url":
        url = source.source_ref.get("url")
        if not url:
            raise ValueError(
                f"Source {source.aizk_uuid}: url source_ref missing 'url':"
                f" {source.source_ref!r}"
            )
        return ManifestProvenanceUrl(url=str(url)), None
    if ingress_kind == "inline_html":
        content_hash = source.source_ref.get("content_hash")
        if not content_hash:
            raise ValueError(
                f"Source {source.aizk_uuid}: inline_html source_ref missing 'content_hash':"
                f" {source.source_ref!r}"
            )
        return (
            ManifestProvenanceInline(content_hash=str(content_hash)),
            None,
        )

    raise ValueError(f"Unknown source_ref kind for manifest provenance: {ingress_kind!r}")


def generate_manifest(
    source: Source,
    job: ConversionJob,
    fetched_at: datetime,
    markdown_s3_uri: str,
    markdown_hash: str,
    figure_s3_uris: list[str],
    docling_version: str,
    pipeline_name: Literal["html", "pdf"],
    *,
    config_snapshot: ManifestConfigSnapshotV2 | ManifestConfigSnapshotV1 | dict[str, Any],
    converter_name: str = "docling",
) -> ConversionManifestV2:
    """Generate a v2.0 manifest for conversion artifacts.

    Accepts either a v1 snapshot dict (backwards-compatible call sites) or a
    v2 ``ManifestConfigSnapshotV2``.  Callers supplying a v1 snapshot are
    auto-migrated to v2 by wrapping the Docling fields under ``adapter``.
    """
    if job.id is None:
        raise ValueError("ConversionJob.id must be set before manifest generation")

    started_at = _coerce_datetime(job.started_at, fetched_at)
    finished_at = _coerce_datetime(job.finished_at, fetched_at)
    duration_seconds = max(0, int((finished_at - started_at).total_seconds()))

    source_type = source.source_type
    if source_type not in {"arxiv", "github", "other"}:
        source_type = None

    figures = [ManifestArtifactFigure(key=uri, created_at=finished_at) for uri in figure_s3_uris]

    # Normalize the snapshot to v2 shape.
    if isinstance(config_snapshot, ManifestConfigSnapshotV2):
        snapshot_v2 = config_snapshot
    elif isinstance(config_snapshot, ManifestConfigSnapshotV1):
        snapshot_v2 = ManifestConfigSnapshotV2(
            converter_name=converter_name,
            adapter=config_snapshot.model_dump(),
        )
    else:
        # Raw dict (legacy callsite passing build_output_config_snapshot output).
        snapshot_v2 = ManifestConfigSnapshotV2(
            converter_name=converter_name,
            adapter=dict(config_snapshot),
        )

    provenance, ingress = _derive_provenance_and_ingress(source)

    return ConversionManifestV2(
        version="2.0",
        aizk_uuid=source.aizk_uuid,
        karakeep_id=source.karakeep_id,
        provenance=provenance,
        ingress=ingress,
        source=ManifestSourceV2(
            url=source.url,
            normalized_url=source.normalized_url,
            title=source.title,
            source_type=source_type,  # type: ignore[arg-type]
            fetched_at=fetched_at,
        ),
        conversion=ManifestConversionMetadata(
            job_id=job.id,
            payload_version=job.payload_version,
            docling_version=docling_version,
            pipeline_name=pipeline_name,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
        ),
        artifacts=ManifestArtifacts(
            markdown=ManifestArtifactMarkdown(
                key=markdown_s3_uri,
                hash_xx64=markdown_hash,
                created_at=finished_at,
            ),
            figures=figures,
        ),
        config_snapshot=snapshot_v2,
    )


def save_manifest(manifest: ConversionManifestV2, output_path: Path) -> None:
    """Save manifest to JSON file.

    Args:
        manifest: ConversionManifest Pydantic model.
        output_path: Path to save manifest.json.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(manifest.model_dump_json(indent=2, exclude_none=True))
    logger.info("Saved manifest to %s", output_path)
