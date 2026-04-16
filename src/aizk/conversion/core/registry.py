"""Fetcher and converter registries for the pluggable conversion pipeline."""

from __future__ import annotations

from typing import Literal

from aizk.conversion.core.errors import FetcherNotRegistered, NoConverterForFormat
from aizk.conversion.core.types import ContentType


Role = Literal["content_fetcher", "resolver"]


class FetcherRegistry:
    """Maps source-ref kinds to either a content fetcher or a ref resolver.

    Role is declared at registration via the entry-point chosen
    (`register_content_fetcher` or `register_resolver`); it is never inferred
    by structural typing. Kind uniqueness is enforced across both roles.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[Role, object]] = {}

    def register_content_fetcher(self, kind: str, impl: object) -> None:
        self._register(kind, "content_fetcher", impl)

    def register_resolver(self, kind: str, impl: object) -> None:
        self._register(kind, "resolver", impl)

    def _register(self, kind: str, role: Role, impl: object) -> None:
        if kind in self._entries:
            existing_role = self._entries[kind][0]
            raise ValueError(
                f"Kind {kind!r} is already registered as {existing_role!r}"
            )
        self._entries[kind] = (role, impl)

    def resolve(self, kind: str) -> tuple[Role, object]:
        """Return (role, impl) for the given kind, or raise `FetcherNotRegistered`."""
        try:
            return self._entries[kind]
        except KeyError:
            raise FetcherNotRegistered(kind) from None

    def registered_kinds(self) -> frozenset[str]:
        """Union of all registered kinds across both roles."""
        return frozenset(self._entries)


class ConverterRegistry:
    """Maps `(ContentType, impl_name)` pairs to converter instances."""

    def __init__(self) -> None:
        self._entries: dict[tuple[ContentType, str], object] = {}

    def register(self, name: str, impl: object) -> None:
        """Register a converter under each format in its `supported_formats`."""
        supported = getattr(impl, "supported_formats", frozenset())
        if not supported:
            raise ValueError(
                f"Converter {name!r} declares no supported_formats; nothing to register"
            )
        for content_type in supported:
            self._entries[(content_type, name)] = impl

    def resolve(self, content_type: ContentType, name: str) -> object:
        """Return the converter for `(content_type, name)`, or raise `NoConverterForFormat`."""
        try:
            return self._entries[(content_type, name)]
        except KeyError:
            raise NoConverterForFormat(content_type, name) from None
