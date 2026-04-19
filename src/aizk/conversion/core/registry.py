"""Fetcher and converter registries for the conversion pipeline.

``FetcherRegistry`` stores adapters keyed by ``SourceRef.kind`` and enforces:
- each kind has exactly one registration across both roles;
- the impl conforms to the role declared at the registration call-site
  (``register_content_fetcher`` / ``register_resolver``);
- the registry state is unchanged when a registration is rejected.

Public-ingress acceptability is NOT derived from registry membership — see
``aizk.conversion.wiring.IngressPolicy``. The registry therefore exposes no
``submittable_kinds()`` concept.
"""

from __future__ import annotations

from aizk.conversion.core.errors import (
    FetcherNotRegistered,
    NoConverterForFormat,
    RegistrationRoleMismatch,
)
from aizk.conversion.core.protocols import ContentFetcher, Converter, RefResolver
from aizk.conversion.core.types import ContentType


class FetcherRegistry:
    """Maps ``SourceRef.kind`` to either a ContentFetcher or a RefResolver."""

    def __init__(self) -> None:
        self._content_fetchers: dict[str, ContentFetcher] = {}
        self._resolvers: dict[str, RefResolver] = {}

    def register_content_fetcher(self, kind: str, impl: ContentFetcher) -> None:
        """Register ``impl`` as the content fetcher for ``kind``.

        Raises ``RegistrationRoleMismatch`` if ``impl`` satisfies ``RefResolver``
        or does not satisfy ``ContentFetcher``; raises ``ValueError`` on a
        duplicate kind (across either role). Registry state is unchanged on
        rejection.
        """
        if isinstance(impl, RefResolver):
            raise RegistrationRoleMismatch(
                f"register_content_fetcher({kind!r}, ...) received an impl that "
                f"satisfies RefResolver; use register_resolver instead"
            )
        if not isinstance(impl, ContentFetcher):
            raise RegistrationRoleMismatch(
                f"register_content_fetcher({kind!r}, ...) received an impl that "
                f"does not satisfy the ContentFetcher protocol"
            )
        self._require_unregistered(kind)
        self._content_fetchers[kind] = impl

    def register_resolver(self, kind: str, impl: RefResolver) -> None:
        """Register ``impl`` as the ref resolver for ``kind``.

        Raises ``RegistrationRoleMismatch`` if ``impl`` does not satisfy
        ``RefResolver``; raises ``ValueError`` on a duplicate kind (across
        either role). Registry state is unchanged on rejection.
        """
        if not isinstance(impl, RefResolver):
            raise RegistrationRoleMismatch(
                f"register_resolver({kind!r}, ...) received an impl that does not satisfy the RefResolver protocol"
            )
        self._require_unregistered(kind)
        self._resolvers[kind] = impl

    def resolve(self, kind: str) -> ContentFetcher | RefResolver:
        """Return the adapter registered for ``kind`` (either role)."""
        if kind in self._resolvers:
            return self._resolvers[kind]
        if kind in self._content_fetchers:
            return self._content_fetchers[kind]
        raise FetcherNotRegistered(f"No fetcher registered for kind {kind!r}")

    def registered_kinds(self) -> frozenset[str]:
        """Return the union of kinds registered across both roles."""
        return frozenset(self._content_fetchers.keys() | self._resolvers.keys())

    def _require_unregistered(self, kind: str) -> None:
        if kind in self._content_fetchers or kind in self._resolvers:
            raise ValueError(f"Kind {kind!r} is already registered")


class ConverterRegistry:
    """Indexes converters by ``(content_type, impl_name)``."""

    def __init__(self) -> None:
        self._converters: dict[tuple[ContentType, str], Converter] = {}

    def register(self, converter: Converter, name: str) -> None:
        """Register ``converter`` under ``(content_type, name)`` for each supported type."""
        for content_type in converter.supported_formats:
            key = (content_type, name)
            if key in self._converters:
                raise ValueError(f"Converter already registered for {content_type!r} under name {name!r}")
            self._converters[key] = converter

    def resolve(self, content_type: ContentType, name: str) -> Converter:
        """Return the converter registered under ``(content_type, name)``."""
        try:
            return self._converters[(content_type, name)]
        except KeyError as exc:
            raise NoConverterForFormat(
                f"No converter registered for content_type={content_type!r}, name={name!r}"
            ) from exc
