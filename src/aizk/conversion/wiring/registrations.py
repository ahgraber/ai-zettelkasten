"""Shared adapter registration helper and chain-closure validator."""

from __future__ import annotations

from aizk.conversion.core.errors import ChainNotTerminated
from aizk.conversion.core.orchestrator import DEFAULT_DEPTH_CAP
from aizk.conversion.core.protocols import RefResolver
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.types import ContentType


def validate_chain_closure(
    fetcher_registry: FetcherRegistry,
    depth_cap: int = DEFAULT_DEPTH_CAP,
) -> None:
    """Walk all resolver-declared edges and verify chain termination.

    For every resolver registered in *fetcher_registry*, walks each kind in its
    ``resolves_to`` set and asserts:

    - Every produced kind is registered (content fetcher or resolver).
    - The declared DAG contains no cycles.
    - No declared path from a resolver to a terminal content fetcher requires
      more hops than *depth_cap* allows.

    *depth_cap* defaults to ``DEFAULT_DEPTH_CAP`` — the same value the
    ``Orchestrator`` uses — so the validator and runtime cannot drift.

    Raises:
        ChainNotTerminated: on missing kind, cycle, or depth-cap violation.
    """
    registered = fetcher_registry.registered_kinds()

    def walk(kind: str, ancestor_path: list[str], depth: int) -> None:
        if depth >= depth_cap:
            raise ChainNotTerminated(
                f"Chain exceeds depth cap {depth_cap}: {' -> '.join(ancestor_path + [kind])}",
                resolver_name=ancestor_path[-1] if ancestor_path else kind,
                cycle_path=ancestor_path + [kind],
            )

        impl = fetcher_registry.resolve(kind)
        if not isinstance(impl, RefResolver):
            return

        # Resolver: inspect declared edges.  resolves_to is ClassVar so always on the class.
        resolves_to: frozenset[str] = getattr(type(impl), "resolves_to", frozenset())
        current_path = ancestor_path + [kind]

        for produced_kind in resolves_to:
            if produced_kind not in registered:
                raise ChainNotTerminated(
                    f"Resolver {kind!r} declares resolves_to kind {produced_kind!r}"
                    " which is not registered",
                    resolver_name=kind,
                    missing_kind=produced_kind,
                )
            if produced_kind in current_path:
                cycle_start = current_path.index(produced_kind)
                cycle = current_path[cycle_start:] + [produced_kind]
                raise ChainNotTerminated(
                    f"Cycle in resolver chain: {' -> '.join(cycle)}",
                    resolver_name=kind,
                    cycle_path=cycle,
                )
            walk(produced_kind, current_path, depth + 1)

    for kind in registered:
        impl = fetcher_registry.resolve(kind)
        if isinstance(impl, RefResolver):
            walk(kind, [], 0)


def _compute_content_type_map(
    fetcher_registry: FetcherRegistry,
) -> dict[str, frozenset[ContentType]]:
    """Compute terminal content types reachable from each registered kind.

    Terminal content types are sourced from each ContentFetcher adapter's
    ``produces`` class attribute — the wiring layer never owns that mapping.
    """
    registered = fetcher_registry.registered_kinds()

    def resolve_types(kind: str, seen: frozenset[str]) -> frozenset[ContentType]:
        if kind in seen:
            return frozenset()
        seen = seen | {kind}
        impl = fetcher_registry.resolve(kind)
        if not isinstance(impl, RefResolver):
            return frozenset(getattr(type(impl), "produces", frozenset()))
        resolves_to: frozenset[str] = getattr(type(impl), "resolves_to", frozenset())
        result: frozenset[ContentType] = frozenset()
        for produced_kind in resolves_to:
            if produced_kind in registered:
                result = result | resolve_types(produced_kind, seen)
        return result

    return {kind: resolve_types(kind, frozenset()) for kind in registered}


def register_ready_adapters(
    fetcher_registry: FetcherRegistry,
    converter_registry: ConverterRegistry,
    cfg: object,
    *,
    include_converters: bool = True,
) -> tuple[dict[str, frozenset[ContentType]], frozenset[ContentType]]:
    """Register all production-ready adapters into *fetcher_registry* and *converter_registry*.

    Called by both worker and API wiring so their ``accepted_kinds`` cannot drift.
    ``SingleFileFetcher`` is intentionally not registered (skeleton, not yet implemented).

    After registrations complete, invokes ``validate_chain_closure`` so process
    startup fails before accepting requests if the declared resolver graph is broken.

    Args:
        fetcher_registry: Registry into which fetchers/resolvers are registered.
        converter_registry: Registry into which converters are registered.
        cfg: ``ConversionConfig`` (or compatible) instance.
        include_converters: When False, skips converter registration (and the
            heavyweight DoclingConverter import).  The API process uses this
            mode; converters are a worker-process concern.

    Returns:
        Tuple of (content_type_map, registered_content_types) for building
        ``DeploymentCapabilities``.
    """
    from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher
    from aizk.conversion.adapters.fetchers.github import GithubReadmeFetcher
    from aizk.conversion.adapters.fetchers.inline import InlineContentFetcher
    from aizk.conversion.adapters.fetchers.karakeep import KarakeepBookmarkResolver
    from aizk.conversion.adapters.fetchers.url import UrlFetcher

    fetcher_registry.register_resolver("karakeep_bookmark", KarakeepBookmarkResolver(config=cfg))
    fetcher_registry.register_content_fetcher("arxiv", ArxivFetcher(config=cfg))
    fetcher_registry.register_content_fetcher("github_readme", GithubReadmeFetcher(config=cfg))
    fetcher_registry.register_content_fetcher("url", UrlFetcher(config=cfg))
    fetcher_registry.register_content_fetcher("inline_html", InlineContentFetcher())

    if include_converters:
        from aizk.conversion.adapters.converters.docling import DoclingConverter

        converter_registry.register("docling", DoclingConverter(cfg))

    validate_chain_closure(fetcher_registry)

    content_type_map = _compute_content_type_map(fetcher_registry)
    registered_content_types = converter_registry.registered_formats()
    return content_type_map, registered_content_types
