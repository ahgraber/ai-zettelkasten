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
    """Terminal fetcher: given a SourceRef, returns raw bytes + content type.

    ``produces`` is the class-level set of ``ContentType``s the fetcher can emit.
    Wiring reads it without instantiating the adapter so the set of terminal
    content types is an adapter-owned declaration, not a separate map in
    ``wiring/``.

    **Wiring convention (not part of the protocol):** adapters also declare a
    class-level ``api_submittable: bool`` attribute which the API wiring reads
    via ``getattr`` to decide which kinds external clients may submit. It is
    intentionally not a Protocol member so adding it does not break structural
    subtyping for lightweight test fakes.
    """

    produces: ClassVar[frozenset[ContentType]]

    def fetch(self, ref: SourceRefVariant) -> ConversionInput: ...


@runtime_checkable
class RefResolver(Protocol):
    """Intermediate resolver: refines a SourceRef into a more specific one.

    ``resolves_to`` is the static edge set used by wiring-time chain-closure
    validation. The orchestrator distinguishes resolvers from content fetchers
    via ``isinstance(impl, RefResolver)``, so this protocol must stay narrow —
    the set of attributes here is what any fake or real resolver must supply.

    **Wiring convention (not part of the protocol):** the ``api_submittable``
    attribute, see :class:`ContentFetcher`.
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
