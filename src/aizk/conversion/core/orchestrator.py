"""Orchestrator: coordinates the fetch-chain and converter dispatch.

The orchestrator is a pure coordinator:
- It does not import adapter modules (depends only on injected callables).
- It holds no global state.
- It determines the dispatch role (ContentFetcher vs RefResolver) structurally
  via ``isinstance(impl, RefResolver)`` — matching the registry invariant so
  declared intent and runtime role cannot diverge.

Stage 2 scope: the GPU ``ResourceGuard`` is NOT entered here. Parent-side
admission control is a Stage 7 concern (see
``.specs/changes/pluggable-fetch-convert/design.md``, "Decision: GPU admission
control stays in the parent process").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass as _dataclass
from typing import Any

from aizk.conversion.core.errors import FetcherDepthExceeded, MissingContentError
from aizk.conversion.core.protocols import ContentFetcher, Converter, RefResolver
from aizk.conversion.core.source_ref import SourceRef
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput, SourceMetadata


@_dataclass(frozen=True)
class ProcessResult:
    """Extended result from process_with_provenance including fetch provenance."""

    artifacts: ConversionArtifacts
    terminal_ref: SourceRef  # the SourceRef whose ContentFetcher produced the bytes
    conversion_input: ConversionInput  # for content_type
    converter_name: str
    config_snapshot: dict[str, Any]


class Orchestrator:
    """Coordinates fetch -> convert for a single job.

    Dependencies are injected as callables so tests can substitute fakes and
    the orchestrator is agnostic to which concrete registries are in use.
    """

    def __init__(
        self,
        resolve_fetcher: Callable[[str], ContentFetcher | RefResolver],
        resolve_converter: Callable[[ContentType, str], Converter],
        *,
        depth_cap: int = 2,
        depth_cap_config_key: str = "AIZK_CONVERSION__FETCHER_DEPTH_CAP",
    ) -> None:
        self._resolve_fetcher = resolve_fetcher
        self._resolve_converter = resolve_converter
        self._depth_cap = depth_cap
        self._depth_cap_config_key = depth_cap_config_key

    def _fetch_with_terminal_ref(
        self,
        ref: SourceRef,
        source_meta: SourceMetadata,
        depth: int = 0,
        kinds_seen: list[str] | None = None,
    ) -> tuple[ConversionInput, SourceRef]:
        """Dispatch ``ref`` through the fetch chain, threading ``source_meta`` across hops.

        Returns the ``ConversionInput`` (which carries the final merged ``source_meta``)
        and the terminal ``SourceRef`` (the ref dispatched to a ``ContentFetcher``).
        """
        if kinds_seen is None:
            kinds_seen = []
        trail = [*kinds_seen, ref.kind]
        impl = self._resolve_fetcher(ref.kind)

        if isinstance(impl, RefResolver):
            if depth >= self._depth_cap:
                raise FetcherDepthExceeded(
                    cap=self._depth_cap,
                    kinds_traversed=trail,
                    config_key=self._depth_cap_config_key,
                )
            refined, meta_new = impl.resolve(ref)
            merged = source_meta.merge(meta_new)
            return self._fetch_with_terminal_ref(refined, merged, depth + 1, trail)

        # ContentFetcher: pass the accumulated source_meta and get back merged input.
        return impl.fetch(ref, source_meta), ref

    def process(self, ref: SourceRef, converter_name: str) -> ConversionArtifacts:
        """Run the full fetch -> convert cycle for ``ref`` using ``converter_name``."""
        conversion_input, _ = self._fetch_with_terminal_ref(ref, SourceMetadata())
        if not conversion_input.content:
            raise MissingContentError(f"Fetcher returned zero-length content for {ref!r}")
        converter = self._resolve_converter(conversion_input.content_type, converter_name)
        return converter.convert(conversion_input)

    def process_with_provenance(self, ref: SourceRef, converter_name: str) -> ProcessResult:
        """Run the full fetch -> convert cycle and return provenance alongside artifacts."""
        conversion_input, terminal_ref = self._fetch_with_terminal_ref(ref, SourceMetadata())
        if not conversion_input.content:
            raise MissingContentError(f"Fetcher returned zero-length content for {ref!r}")
        converter = self._resolve_converter(conversion_input.content_type, converter_name)
        artifacts = converter.convert(conversion_input)
        config_snapshot = converter.config_snapshot()
        return ProcessResult(
            artifacts=artifacts,
            terminal_ref=terminal_ref,
            conversion_input=conversion_input,
            converter_name=converter_name,
            config_snapshot=config_snapshot,
        )
