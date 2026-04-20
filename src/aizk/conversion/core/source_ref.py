"""SourceRef discriminated union and dedup-payload hashing.

Each variant carries only the fields needed to fetch its content. The
`to_dedup_payload()` method on each variant returns the canonical,
normalized identity dict used for Source dedup; cosmetic fields (e.g.,
``ArxivRef.arxiv_pdf_url``, ``GithubReadmeRef.branch``) are excluded so
that the hash stays stable across incidental variation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 64 KiB enforced on RAW body bytes (not serialized JSON length).
# Typical HTML escaping expands ~1.3x, so the persisted JSON column can be
# up to ~85 KiB — SQLite handles this comfortably.
_INLINE_HTML_MAX_BYTES = 64 * 1024


class KarakeepBookmarkRef(BaseModel):
    """Reference to a KaraKeep bookmark; resolved to a more specific ref by the resolver."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["karakeep_bookmark"] = "karakeep_bookmark"
    bookmark_id: str

    def to_dedup_payload(self) -> dict:
        """Return the canonical identity payload: ``{"kind", "bookmark_id"}``."""
        return {"kind": self.kind, "bookmark_id": self.bookmark_id}


class ArxivRef(BaseModel):
    """Reference to an arXiv paper."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["arxiv"] = "arxiv"
    arxiv_id: str
    arxiv_pdf_url: str | None = None

    def to_dedup_payload(self) -> dict:
        """Return the canonical identity payload: ``{"kind", "arxiv_id"}`` (pdf_url excluded)."""
        # arxiv_pdf_url is a cosmetic hint for the fetcher, not part of identity.
        return {"kind": self.kind, "arxiv_id": self.arxiv_id.strip()}


class GithubReadmeRef(BaseModel):
    """Reference to a GitHub repo README.

    ``branch`` is accepted for forward compatibility but currently ignored by
    ``GithubReadmeFetcher``, which hardcodes a ``main``/``master`` fallback.
    Wiring ``branch`` through to the fetcher is deferred until ``IngressPolicy``
    widens to admit ``github_readme`` for public submission. The field is also
    excluded from ``to_dedup_payload()`` so identity is ``(owner, repo)``.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["github_readme"] = "github_readme"
    owner: str
    repo: str
    branch: str | None = None

    def to_dedup_payload(self) -> dict:
        """Return the canonical identity payload: ``{"kind", "owner", "repo"}`` (branch excluded)."""
        # branch is fetch metadata; identity is (owner, repo).
        return {"kind": self.kind, "owner": self.owner, "repo": self.repo}


class UrlRef(BaseModel):
    """Reference to a URL. The URL is stored in normalized form for stable identity."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["url"] = "url"
    url: str

    @field_validator("url", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        # Apply canonical URL normalization so dedup identity is stable across
        # cosmetic variation (scheme case, trailing slash, UTM params, www).
        # Falls back to whitespace strip + scheme/host lowercase when the input
        # is not a recognizable URL (e.g., placeholder fixtures).
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        try:
            from aizk.utilities.url_utils import normalize_url
        except Exception:
            return stripped
        try:
            return normalize_url(stripped)
        except Exception:
            return stripped

    def to_dedup_payload(self) -> dict:
        """Return the canonical identity payload: ``{"kind", "url"}`` (already normalized)."""
        return {"kind": self.kind, "url": self.url}


class SingleFileRef(BaseModel):
    """Reference to a SingleFile-archived page.

    Skeleton variant: unused at cutover; kept in the union so future wiring
    does not require a SourceRef migration.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["singlefile"] = "singlefile"
    url: str

    def to_dedup_payload(self) -> dict:
        """Return the canonical identity payload: ``{"kind", "url"}``."""
        return {"kind": self.kind, "url": self.url.strip()}


class InlineHtmlRef(BaseModel):
    """Reference carrying inline HTML/text bytes directly, capped at 64 KiB raw."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["inline_html"] = "inline_html"
    body: bytes

    @field_validator("body")
    @classmethod
    def _enforce_size_cap(cls, value: bytes) -> bytes:
        # Cap is measured on raw body bytes, NOT on serialized JSON length,
        # because JSON escaping would otherwise penalize ordinary content.
        if len(value) > _INLINE_HTML_MAX_BYTES:
            raise ValueError(f"InlineHtmlRef body exceeds {_INLINE_HTML_MAX_BYTES} bytes (got {len(value)} bytes)")
        return value

    def to_dedup_payload(self) -> dict:
        """Return the canonical identity payload: content-addressed sha256 of the body."""
        # Content-addressed: dedup payload stores the sha256 of body bytes,
        # never the bytes themselves.
        return {
            "kind": self.kind,
            "content_hash": hashlib.sha256(self.body).hexdigest(),
        }


SourceRef = Annotated[
    Union[
        KarakeepBookmarkRef,
        ArxivRef,
        GithubReadmeRef,
        UrlRef,
        SingleFileRef,
        InlineHtmlRef,
    ],
    Field(discriminator="kind"),
]


def compute_source_ref_hash(ref: "SourceRef") -> str:
    """Compute the canonical SHA-256 hash of a SourceRef's dedup payload."""
    payload = ref.to_dedup_payload()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
