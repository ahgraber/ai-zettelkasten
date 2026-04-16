"""Orchestrator: coordinates the fetch → convert pipeline via injected callables."""

from __future__ import annotations

from typing import Callable

from aizk.conversion.core.errors import FetcherDepthExceeded
from aizk.conversion.core.source_ref import SourceRefVariant
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput


class Orchestrator:
    """Coordinates fetch → convert via dependency-injected callables.

    Parameters
    ----------
    resolve_fetcher:
        Callable mapping ``kind`` → ``(role, impl)`` where role is
        ``"content_fetcher"`` or ``"resolver"``.
    resolve_converter:
        Callable mapping ``(ContentType, name)`` → converter instance.
    depth_cap:
        Maximum resolver recursion depth.  Raises ``FetcherDepthExceeded``
        once the chain depth reaches this value.
    """

    def __init__(
        self,
        resolve_fetcher: Callable[[str], tuple[str, object]],
        resolve_converter: Callable[[ContentType, str], object],
        depth_cap: int = 3,
    ) -> None:
        self._resolve_fetcher = resolve_fetcher
        self._resolve_converter = resolve_converter
        self._depth_cap = depth_cap

    def _fetch(self, ref: SourceRefVariant, depth: int) -> ConversionInput:
        if depth >= self._depth_cap:
            raise FetcherDepthExceeded(depth=depth, kind=ref.kind)
        role, impl = self._resolve_fetcher(ref.kind)
        if role == "content_fetcher":
            return impl.fetch(ref)  # type: ignore[union-attr]
        resolved_ref = impl.resolve(ref)  # type: ignore[union-attr]
        return self._fetch(resolved_ref, depth + 1)

    def process(self, ref: SourceRefVariant, converter_name: str) -> ConversionArtifacts:
        """Fetch content for *ref*, then convert it with the named converter."""
        conv_input = self._fetch(ref, depth=0)
        converter = self._resolve_converter(conv_input.content_type, converter_name)
        return converter.convert(conv_input)  # type: ignore[union-attr]
