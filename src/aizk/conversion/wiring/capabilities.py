"""Deployment and submission capability descriptors for the conversion pipeline.

``DeploymentCapabilities`` describes what a worker node can actually do (which
kinds are registered, which content types are reachable, which converters are
available).

``SubmissionCapabilities`` describes what the API layer exposes to external
callers (a subset governed by ``IngressPolicy``).

These two descriptors are intentionally separate: deployment capability is
derived from registry state; submission capability is a deployment policy
choice. They diverge by design.
"""

from __future__ import annotations

from typing import Any

from aizk.conversion.core.protocols import RefResolver
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.types import ContentType

# Probe type — deferred to Stage 7. Placeholder for startup health-check callables.
Probe = Any


class DeploymentCapabilities:
    """Worker-side capability descriptor. Sourced from FetcherRegistry + ConverterRegistry."""

    def __init__(self, fetcher_registry: FetcherRegistry, converter_registry: ConverterRegistry) -> None:
        self._fr = fetcher_registry
        self._cr = converter_registry

    @property
    def registered_kinds(self) -> frozenset[str]:
        """Return all kinds registered in the fetcher registry."""
        return self._fr.registered_kinds()

    def content_types_for(self, kind: str) -> frozenset[ContentType]:
        """Recursively walk the resolver chain to find terminal content types.

        For a ``ContentFetcher``, returns its ``produces`` set directly.
        For a ``RefResolver``, recurses into each kind it declares in
        ``resolves_to`` (if that kind is also registered) and unions the
        results.
        """
        impl = self._fr.resolve(kind)
        if isinstance(impl, RefResolver):
            result: set[ContentType] = set()
            for target in impl.resolves_to:
                if target in self._fr.registered_kinds():
                    result.update(self.content_types_for(target))
            return frozenset(result)
        # Structural fallthrough: ContentFetcher
        return impl.produces  # type: ignore[return-value]

    def converter_available(self, content_type: ContentType) -> bool:
        """Return True if any converter is registered for ``content_type``."""
        return self._cr.has_converter_for(content_type)

    @property
    def startup_probes(self) -> list[Probe]:
        """Return startup health-check probes. Deferred to Stage 7."""
        return []


class SubmissionCapabilities:
    """API-side capability descriptor. Sourced from IngressPolicy.

    Describes which submission kinds the API layer will accept from external
    callers. This is a policy decision, not a capability one.
    """

    def __init__(self, accepted_submission_kinds: frozenset[str]) -> None:
        self.accepted_submission_kinds = accepted_submission_kinds


__all__ = ["DeploymentCapabilities", "Probe", "SubmissionCapabilities"]
