"""SourceRef discriminated union representing all supported ingress paths."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


_MAX_INLINE_BYTES = 64 * 1024  # 64 KB


class _StorageMixin:
    """Default ``to_storage_payload`` for variants whose JSON dump is sufficient.

    Uses ``model_dump(mode="json")`` so non-JSON-primitive fields (e.g. ``bytes``)
    are encoded in a round-trippable form. Variants that persist derived identity
    fields (e.g. ``InlineHtmlRef.content_hash``) override this to add them.
    """

    def to_storage_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")  # type: ignore[attr-defined]


class KarakeepBookmarkRef(_StorageMixin, BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["karakeep_bookmark"] = "karakeep_bookmark"
    bookmark_id: str

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "karakeep_bookmark", "bookmark_id": self.bookmark_id}


class ArxivRef(_StorageMixin, BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["arxiv"] = "arxiv"
    arxiv_id: str
    arxiv_pdf_url: str | None = None
    karakeep_asset_url: str | None = None

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "arxiv", "arxiv_id": self.arxiv_id}


class GithubReadmeRef(_StorageMixin, BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["github_readme"] = "github_readme"
    owner: str
    repo: str

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "github_readme", "owner": self.owner, "repo": self.repo}


class UrlRef(_StorageMixin, BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["url"] = "url"
    url: str

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "url", "url": self.url}


class SingleFileRef(_StorageMixin, BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["singlefile"] = "singlefile"
    path: str

    def to_dedup_payload(self) -> dict[str, object]:
        return {"kind": "singlefile", "path": self.path}


class InlineHtmlRef(_StorageMixin, BaseModel):
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

    def to_storage_payload(self) -> dict[str, Any]:
        # The manifest writer reads ``content_hash`` from the stored payload;
        # ``model_dump`` alone only emits ``body``. Persisting the derived hash
        # keeps the manifest writer schema-agnostic and lets the column be
        # queried by hash without rehydrating the body.
        payload = self.model_dump(mode="json")
        payload["content_hash"] = hashlib.sha256(self.body).hexdigest()
        return payload


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


_SOURCE_REF_ADAPTER: TypeAdapter[SourceRefVariant] = TypeAdapter(SourceRef)


def parse_source_ref(payload: dict[str, Any]) -> SourceRefVariant:
    """Deserialize a dict payload (e.g. from the ``Source.source_ref`` JSON column)
    into the matching ``SourceRef`` variant via the discriminator."""
    return _SOURCE_REF_ADAPTER.validate_python(payload)
