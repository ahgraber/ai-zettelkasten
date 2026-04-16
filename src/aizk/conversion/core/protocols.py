"""Protocol definitions (ports) for the pluggable conversion pipeline.

These protocols are the *only* contract that the orchestrator depends on.
Concrete adapter modules implement these in `aizk.conversion.adapters`.
"""

from __future__ import annotations

from types import TracebackType
from typing import ClassVar, Protocol, runtime_checkable

from aizk.conversion.core.source_ref import SourceRefVariant
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput


@runtime_checkable
class ContentFetcher(Protocol):
    """Terminal fetcher: given a SourceRef, returns raw bytes + content type."""

    def fetch(self, ref: SourceRefVariant) -> ConversionInput: ...


@runtime_checkable
class RefResolver(Protocol):
    """Intermediate resolver: refines a SourceRef into a more specific one.

    The class-level `resolves_to` attribute is the static edge set used by
    wiring-time chain-closure validation.
    """

    resolves_to: ClassVar[frozenset[str]]

    def resolve(self, ref: SourceRefVariant) -> SourceRefVariant: ...


@runtime_checkable
class Converter(Protocol):
    """Converts a ConversionInput into ConversionArtifacts.

    `supported_formats` and `requires_gpu` are class-level so the registry and
    GPU-guard logic can inspect them without instantiating the adapter.
    """

    supported_formats: ClassVar[frozenset[ContentType]]
    requires_gpu: ClassVar[bool]

    def convert(self, conversion_input: ConversionInput) -> ConversionArtifacts: ...

    def config_snapshot(self) -> dict[str, object]: ...


@runtime_checkable
class ResourceGuard(Protocol):
    """Context manager bounding access to a shared resource (e.g., GPU slots).

    The acquiring thread is the sole releaser: the guard must be held for
    the full subprocess lifecycle and released only when the acquiring
    thread's `with` block unwinds.
    """

    def __enter__(self) -> ResourceGuard: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...
