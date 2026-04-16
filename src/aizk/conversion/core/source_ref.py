"""SourceRef discriminated union representing all supported ingress paths."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_MAX_INLINE_BYTES = 64 * 1024  # 64 KB


class KarakeepBookmarkRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["karakeep_bookmark"] = "karakeep_bookmark"
    bookmark_id: str

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "karakeep_bookmark", "bookmark_id": self.bookmark_id}


class ArxivRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["arxiv"] = "arxiv"
    arxiv_id: str
    arxiv_pdf_url: str | None = None
    karakeep_asset_url: str | None = None

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "arxiv", "arxiv_id": self.arxiv_id}


class GithubReadmeRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["github_readme"] = "github_readme"
    owner: str
    repo: str

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "github_readme", "owner": self.owner, "repo": self.repo}


class UrlRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["url"] = "url"
    url: str

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "url", "url": self.url}


class SingleFileRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["singlefile"] = "singlefile"
    path: str

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "singlefile", "path": self.path}


class InlineHtmlRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["inline_html"] = "inline_html"
    body: bytes

    @field_validator("body")
    @classmethod
    def _check_size(cls, v: bytes) -> bytes:
        if len(v) > _MAX_INLINE_BYTES:
            raise ValueError(
                f"InlineHtmlRef body exceeds the 64KB cap ({len(v)} bytes)"
            )
        return v

    def to_dedup_payload(self) -> dict[str, object]:
        return {
            "kind": "inline_html",
            "content_hash": hashlib.sha256(self.body).hexdigest(),
        }


SourceRefVariant = (
    KarakeepBookmarkRef
    | ArxivRef
    | GithubReadmeRef
    | UrlRef
    | SingleFileRef
    | InlineHtmlRef
)

SourceRef = Annotated[SourceRefVariant, Field(discriminator="kind")]


def compute_source_ref_hash(ref: SourceRefVariant) -> str:
    """SHA-256 of the ref's canonical dedup payload.

    The payload is encoded with `sort_keys=True` and compact separators so that
    field declaration order, optional-default appearance, and future additive
    fields do not cause hash churn.
    """
    payload = ref.to_dedup_payload()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
