"""Fetcher and converter registries for the pluggable conversion pipeline."""

from __future__ import annotations

from aizk.conversion.core.errors import FetcherNotRegistered, NoConverterForFormat
from aizk.conversion.core.types import ContentType


class FetcherRegistry:
    """Maps source-ref kinds to either a content fetcher or a ref resolver.

    Role is distinguished structurally via ``isinstance(impl, RefResolver)`` at
    dispatch time — the registry itself stores a single ``impl`` per kind.
    Kind uniqueness is enforced across the combined set. The two entry points
    (``register_content_fetcher`` / ``register_resolver``) remain as
    registration-intent documentation and keep the call-site readable.
    """

    def __init__(self) -> None:
        self._entries: dict[str, object] = {}

    def register_content_fetcher(self, kind: str, impl: object) -> None:
        self._register(kind, impl)

    def register_resolver(self, kind: str, impl: object) -> None:
        self._register(kind, impl)

    def _register(self, kind: str, impl: object) -> None:
        if kind in self._entries:
            raise ValueError(f"Kind {kind!r} is already registered")
        self._entries[kind] = impl

    def resolve(self, kind: str) -> object:
        """Return the adapter for the given kind, or raise ``FetcherNotRegistered``."""
        try:
            return self._entries[kind]
        except KeyError:
            raise FetcherNotRegistered(kind) from None

    def registered_kinds(self) -> frozenset[str]:
        """Union of all registered kinds."""
        return frozenset(self._entries)

    def submittable_kinds(self) -> frozenset[str]:
        """Subset of registered kinds whose adapters declare ``api_submittable == True``.

        This is the authoritative set of kinds the public API may accept as
        ingress. Worker-internal kinds (e.g. ``arxiv``, ``inline_html``) are
        still registered for chain-closure validation but their adapters
        declare ``api_submittable = False`` so they cannot be directly
        submitted by external clients.
        """
        return frozenset(
            kind
            for kind, impl in self._entries.items()
            if getattr(type(impl), "api_submittable", False)
        )


class ConverterRegistry:
    """Maps `(ContentType, impl_name)` pairs to converter instances."""

    def __init__(self) -> None:
        self._entries: dict[tuple[ContentType, str], object] = {}

    def register(self, name: str, impl: object) -> None:
        """Register a converter under each format in its `supported_formats`."""
        if not hasattr(impl, "supported_formats"):
            raise ValueError(
                f"Converter {name!r} has no `supported_formats` attribute"
            )
        supported = impl.supported_formats  # type: ignore[union-attr]
        if not supported:
            raise ValueError(
                f"Converter {name!r} declares an empty `supported_formats`; nothing to register"
            )
        for content_type in supported:
            self._entries[(content_type, name)] = impl

    def resolve(self, content_type: ContentType, name: str) -> object:
        """Return the converter for `(content_type, name)`, or raise `NoConverterForFormat`."""
        try:
            return self._entries[(content_type, name)]
        except KeyError:
            raise NoConverterForFormat(content_type, name) from None

    def get_by_name(self, name: str) -> object | None:
        """Return a single converter instance registered under *name*, or ``None``.

        A converter is registered under one ``(ContentType, name)`` key per
        supported format, but the same ``impl`` instance is stored under every
        key. This lookup returns that single instance so callers that need
        class-level attributes (e.g. ``requires_gpu``) can read them without
        knowing which content type to query.
        """
        for (_content_type, reg_name), impl in self._entries.items():
            if reg_name == name:
                return impl
        return None

    def registered_formats(self) -> frozenset[ContentType]:
        """Content types for which at least one converter is registered."""
        return frozenset(ct for ct, _ in self._entries)
