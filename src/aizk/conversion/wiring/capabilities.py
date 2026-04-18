"""DeploymentCapabilities descriptor for the conversion pipeline."""

from __future__ import annotations

from typing import Callable

from aizk.conversion.core.types import ContentType

Probe = Callable[[], None]


class DeploymentCapabilities:
    """Describes what a deployed process can accept and convert.

    Attributes:
        accepted_kinds: Source-ref kinds the wiring layer has registered.
            Exactly the set returned by ``FetcherRegistry.registered_kinds()``.
        startup_probes: Zero-argument callables run at process startup to
            verify adapter connectivity.
    """

    def __init__(
        self,
        accepted_kinds: frozenset[str],
        content_type_map: dict[str, frozenset[ContentType]],
        registered_content_types: frozenset[ContentType],
        startup_probes: list[Probe],
    ) -> None:
        self.accepted_kinds = accepted_kinds
        self._content_type_map = content_type_map
        self._registered_content_types = registered_content_types
        self.startup_probes = startup_probes

    def content_types_for(self, kind: str) -> frozenset[ContentType]:
        """Terminal content types the pipeline can produce for a given source kind."""
        return self._content_type_map.get(kind, frozenset())

    def converter_available(self, content_type: ContentType) -> bool:
        """True when a converter is registered for the given content type."""
        return content_type in self._registered_content_types
