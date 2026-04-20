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
import logging
from typing import Any

from aizk.conversion.core.errors import FetcherDepthExceeded
from aizk.conversion.core.protocols import ContentFetcher, Converter, RefResolver
from aizk.conversion.core.source_ref import SourceRef
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput

logger = logging.getLogger(__name__)


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

    def _fetch(self, ref: SourceRef, depth: int = 0) -> ConversionInput:
        """Dispatch ``ref`` through the fetcher chain, recursing on resolvers.

        Role is determined structurally: a ``RefResolver`` refines the ref and
        the orchestrator recurses with ``depth + 1``; a ``ContentFetcher`` is
        terminal and returns a ``ConversionInput``.
        """
        return self._fetch_with_trail(ref, depth, [])

    def _fetch_with_trail(
        self,
        ref: SourceRef,
        depth: int,
        kinds_seen: list[str],
    ) -> ConversionInput:
        # Record the kind at this dispatch attempt BEFORE resolving, so the
        # depth-cap error message names the kind that triggered the violation.
        trail = [*kinds_seen, ref.kind]
        impl = self._resolve_fetcher(ref.kind)

        if isinstance(impl, RefResolver):
            # Depth-cap check fires ONLY when we would recurse. A terminal
            # ContentFetcher at any depth is allowed to return.
            if depth >= self._depth_cap:
                raise FetcherDepthExceeded(
                    cap=self._depth_cap,
                    kinds_traversed=trail,
                    config_key=self._depth_cap_config_key,
                )
            refined = impl.resolve(ref)
            return self._fetch_with_trail(refined, depth + 1, trail)

        # Structural fallthrough: not a RefResolver, so it must be a
        # ContentFetcher (the registry guarantees exactly these two roles).
        return impl.fetch(ref)

    def _fetch_with_terminal_ref(
        self,
        ref: SourceRef,
        depth: int = 0,
        kinds_seen: list[str] | None = None,
    ) -> tuple[ConversionInput, SourceRef]:
        """Like ``_fetch_with_trail`` but also returns the terminal ref.

        The terminal ref is the ``SourceRef`` that was dispatched to a
        ``ContentFetcher`` (as opposed to a ``RefResolver``).
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
            refined = impl.resolve(ref)
            return self._fetch_with_terminal_ref(refined, depth + 1, trail)

        # ContentFetcher: this ref is the terminal ref.
        return impl.fetch(ref), ref

    def process(self, ref: SourceRef, converter_name: str) -> ConversionArtifacts:
        """Run the full fetch -> convert cycle for ``ref`` using ``converter_name``."""
        conversion_input = self._fetch(ref)
        converter = self._resolve_converter(conversion_input.content_type, converter_name)
        return converter.convert(conversion_input)

    def process_with_provenance(self, ref: SourceRef, converter_name: str) -> ProcessResult:
        """Like ``process`` but also returns fetch provenance (terminal ref and conversion input)."""
        conversion_input, terminal_ref = self._fetch_with_terminal_ref(ref)
        converter = self._resolve_converter(conversion_input.content_type, converter_name)
        artifacts = converter.convert(conversion_input)
        if hasattr(converter, "config_snapshot"):
            config_snapshot = converter.config_snapshot()
        else:
            logger.debug("converter %r has no config_snapshot(); recording empty snapshot", converter_name)
            config_snapshot = {}
        return ProcessResult(
            artifacts=artifacts,
            terminal_ref=terminal_ref,
            conversion_input=conversion_input,
            converter_name=converter_name,
            config_snapshot=config_snapshot,
        )
