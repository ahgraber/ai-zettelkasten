"""Protocol definitions (ports) for the pluggable conversion pipeline.

These protocols are the *only* contract that the orchestrator depends on.
Concrete adapter modules implement these in `aizk.conversion.adapters`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import ClassVar, Protocol, runtime_checkable

from aizk.conversion.core.source_ref import (
    ArxivRef,
    GithubReadmeRef,
    InlineHtmlRef,
    KarakeepBookmarkRef,
    SingleFileRef,
    UrlRef,
)
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput

_AnyRef = (
    KarakeepBookmarkRef
    | ArxivRef
    | GithubReadmeRef
    | UrlRef
    | SingleFileRef
    | InlineHtmlRef
)


@runtime_checkable
class ContentFetcher(Protocol):
    """Terminal fetcher: given a SourceRef, returns raw bytes + content type."""

    def fetch(self, ref: _AnyRef) -> ConversionInput: ...


@runtime_checkable
class RefResolver(Protocol):
    """Intermediate resolver: refines a SourceRef into a more specific one.

    The class-level `resolves_to` attribute is the static edge set used by
    wiring-time chain-closure validation.
    """

    resolves_to: ClassVar[frozenset[str]]

    def resolve(self, ref: _AnyRef) -> _AnyRef: ...


@runtime_checkable
class Converter(Protocol):
    """Converts a ConversionInput into ConversionArtifacts.

    `supported_formats` and `requires_gpu` are class-level so the registry and
    GPU-guard logic can inspect them without instantiating the adapter.
    """

    supported_formats: ClassVar[frozenset[ContentType]]
    requires_gpu: ClassVar[bool]

    def convert(self, input: ConversionInput) -> ConversionArtifacts: ...

    def config_snapshot(self) -> dict[str, object]: ...


class ResourceGuard(AbstractContextManager):
    """Context manager bounding access to a shared resource (e.g., GPU slots).

    The acquiring thread is the sole releaser: implementations must release
    only when the acquiring thread's `with` block unwinds.
    """
