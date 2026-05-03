"""Ports (protocols) for the conversion pipeline.

ResourceGuard contract: the acquiring thread is the sole releaser. The guard
is held for the full subprocess lifecycle (spawn, supervise, reap) and
released only when the acquiring thread's ``with`` block unwinds. Supervision
code SHALL NOT release the guard on behalf of the acquiring thread.

Public-ingress acceptability is NOT an adapter concern — no fetcher protocol
carries an ``api_submittable`` / public-ingress flag. It is a deployment
policy enforced by wiring via ``IngressPolicy``.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol, runtime_checkable

from aizk.conversion.core.source_ref import SourceRef
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput, SourceMetadata


@runtime_checkable
class ContentFetcher(Protocol):
    """Terminal fetcher: takes a ref and source metadata, returns the fetched content."""

    produces: ClassVar[frozenset[ContentType]]

    def fetch(self, ref: SourceRef, source_meta: SourceMetadata) -> ConversionInput:
        """Fetch bytes for ``ref``, merge any observed metadata into ``source_meta``, and return both."""
        ...


@runtime_checkable
class RefResolver(Protocol):
    """Intermediate fetcher: refines a ref into a more specific ref and observed source metadata."""

    resolves_to: ClassVar[frozenset[str]]

    def resolve(self, ref: SourceRef) -> tuple[SourceRef, SourceMetadata]:
        """Resolve ``ref`` to a more specific ``SourceRef`` and the metadata observed during resolution."""
        ...


@runtime_checkable
class Converter(Protocol):
    """Capability-indexed converter: declares supported formats and GPU requirement."""

    supported_formats: ClassVar[frozenset[ContentType]]
    requires_gpu: ClassVar[bool]

    def convert(self, input: ConversionInput) -> ConversionArtifacts:  # noqa: A002 — spec argument name
        """Convert ``input`` bytes into markdown and related artifacts."""
        ...

    def config_snapshot(self) -> dict[str, Any]:
        """Return the output-affecting configuration contributed to the idempotency key."""
        ...


@runtime_checkable
class ResourceGuard(Protocol):
    """Context-manager admission gate (e.g., GPU semaphore)."""

    def __enter__(self) -> "ResourceGuard":
        """Acquire the guard; the acquiring thread is the sole releaser."""
        ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None:
        """Release the guard when the acquiring thread's ``with`` block unwinds."""
        ...
