"""Orchestrator: coordinates the fetch → convert pipeline via injected callables."""

from __future__ import annotations

from typing import Callable

DEFAULT_DEPTH_CAP: int = 3
"""Default maximum resolver hops before a terminal content fetch.

Shared by the Orchestrator and wiring-time validate_chain_closure so the
two never drift.  A chain of N resolvers followed by one content fetcher
requires DEFAULT_DEPTH_CAP > N.
"""

from aizk.conversion.core.errors import FetcherDepthExceeded
from aizk.conversion.core.protocols import ContentFetcher, Converter, RefResolver
from aizk.conversion.core.source_ref import SourceRefVariant
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput


class Orchestrator:
    """Coordinates fetch → convert via dependency-injected callables.

    Parameters
    ----------
    resolve_fetcher:
        Callable mapping ``kind`` → ``(role, impl)`` where role is
        ``"content_fetcher"`` or ``"resolver"`` and ``impl`` satisfies
        ``ContentFetcher`` or ``RefResolver`` respectively.
    resolve_converter:
        Callable mapping ``(ContentType, name)`` → a ``Converter`` instance.
    depth_cap:
        Maximum number of resolver hops before the terminal content fetch.
        A chain of *N* resolvers followed by one content fetcher requires
        ``depth_cap > N``; the depth counter is incremented on each resolver
        hop and checked before the next call.  Default 3 allows up to 2
        resolver hops before the terminal fetch.
    """

    def __init__(
        self,
        resolve_fetcher: Callable[[str], tuple[str, ContentFetcher | RefResolver]],
        resolve_converter: Callable[[ContentType, str], Converter],
        depth_cap: int = DEFAULT_DEPTH_CAP,
    ) -> None:
        self._resolve_fetcher = resolve_fetcher
        self._resolve_converter = resolve_converter
        self._depth_cap = depth_cap

    def fetch(self, ref: SourceRefVariant) -> ConversionInput:
        """Fetch content for *ref*, following resolver hops as needed."""
        return self._fetch(ref, depth=0)

    def _fetch(self, ref: SourceRefVariant, depth: int) -> ConversionInput:
        if depth >= self._depth_cap:
            raise FetcherDepthExceeded(depth=depth, kind=ref.kind)
        role, impl = self._resolve_fetcher(ref.kind)
        if role == "content_fetcher":
            return impl.fetch(ref)  # type: ignore[union-attr]
        if role == "resolver":
            resolved_ref = impl.resolve(ref)  # type: ignore[union-attr]
            return self._fetch(resolved_ref, depth + 1)
        raise ValueError(f"unknown fetcher role {role!r} for kind {ref.kind!r}")

    def process(self, ref: SourceRefVariant, converter_name: str) -> ConversionArtifacts:
        """Fetch content for *ref*, then convert it with the named converter."""
        conv_input = self._fetch(ref, depth=0)
        converter = self._resolve_converter(conv_input.content_type, converter_name)
        return converter.convert(conv_input)
