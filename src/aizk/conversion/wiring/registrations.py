"""Adapter registration and resolver chain validation for the conversion pipeline.

``register_ready_adapters`` is the single call-site that populates
``FetcherRegistry`` and ``ConverterRegistry`` with all production-ready
adapters. ``SingleFileFetcher`` is intentionally excluded (skeleton, not ready).

``validate_chain_closure`` validates the resolver DAG at startup: every kind
in a resolver's ``resolves_to`` must be registered, there must be no cycles,
and no path may exceed ``depth_cap`` resolver hops.
"""

from __future__ import annotations

from aizk.conversion.adapters.converters.docling import DoclingConverter
from aizk.conversion.adapters.fetchers.arxiv import ArxivFetcher
from aizk.conversion.adapters.fetchers.github import GithubReadmeFetcher
from aizk.conversion.adapters.fetchers.inline import InlineContentFetcher
from aizk.conversion.adapters.fetchers.karakeep import KarakeepBookmarkResolver
from aizk.conversion.adapters.fetchers.url import UrlFetcher
from aizk.conversion.core.errors import ChainNotTerminated
from aizk.conversion.core.protocols import RefResolver
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig


def register_ready_adapters(
    fetcher_registry: FetcherRegistry,
    converter_registry: ConverterRegistry,
    cfg: ConversionConfig,
    *,
    docling_cfg: DoclingConverterConfig,
    karakeep_cfg: KarakeepFetcherConfig,
) -> None:
    """Populate registries with all production-ready adapters.

    Registers fetchers and resolvers in the ``fetcher_registry`` and the
    Docling converter in the ``converter_registry``. ``SingleFileFetcher`` is
    intentionally excluded (skeleton, raises ``NotImplementedError``).

    Calls ``validate_chain_closure`` after all registrations; raises
    ``ChainNotTerminated`` if the resolver DAG is broken.

    Args:
        fetcher_registry: Registry to populate with fetchers and resolvers.
        converter_registry: Registry to populate with converters.
        cfg: Conversion configuration for adapters that require it.
        docling_cfg: Docling-specific converter configuration.
        karakeep_cfg: KaraKeep-specific fetcher configuration.
    """
    # Resolvers
    fetcher_registry.register_resolver("karakeep_bookmark", KarakeepBookmarkResolver(karakeep_cfg))

    # Content fetchers
    fetcher_registry.register_content_fetcher("arxiv", ArxivFetcher(cfg, karakeep_cfg))
    fetcher_registry.register_content_fetcher("github_readme", GithubReadmeFetcher(cfg))
    fetcher_registry.register_content_fetcher("url", UrlFetcher(cfg, karakeep_cfg))
    fetcher_registry.register_content_fetcher("inline_html", InlineContentFetcher())

    # Converters
    converter_registry.register(DoclingConverter(docling_cfg), "docling")

    # Validate resolver DAG before returning
    validate_chain_closure(fetcher_registry, depth_cap=2)


def validate_chain_closure(fetcher_registry: FetcherRegistry, *, depth_cap: int) -> None:
    """Validate the resolver DAG for completeness, acyclicity, and depth cap.

    For every resolver in the registry:
    - Each kind in ``resolver.resolves_to`` must be registered.
    - There must be no cycles in the resolver graph.
    - No path may exceed ``depth_cap`` resolver hops.

    Args:
        fetcher_registry: The populated fetcher registry to validate.
        depth_cap: Maximum number of resolver hops allowed in any chain.

    Raises:
        ChainNotTerminated: On missing registration, cycle, or depth-cap violation.
    """
    registered = fetcher_registry.registered_kinds()

    # Collect all resolver kinds for cycle/depth DFS
    resolver_kinds: list[str] = [
        kind for kind in registered if isinstance(fetcher_registry.resolve(kind), RefResolver)
    ]

    def _dfs(kind: str, start_kind: str, visited: frozenset[str], depth: int) -> None:
        impl = fetcher_registry.resolve(kind)
        if not isinstance(impl, RefResolver):
            # Terminal fetcher — chain is valid at this branch.
            return

        for target in impl.resolves_to:
            # Check: target must be registered
            if target not in registered:
                raise ChainNotTerminated(
                    f"Resolver for {kind!r} declares resolves_to {target!r} which is not registered"
                )

            # Check: cycle detection
            if target in visited:
                raise ChainNotTerminated(f"Resolver chain has a cycle involving {target!r}")

            # Check: depth cap (depth here is the number of resolver hops taken so far)
            # depth is incremented when we follow a resolver edge; the check is
            # depth > depth_cap, matching the orchestrator's semantics exactly.
            next_depth = depth + 1
            if next_depth > depth_cap:
                raise ChainNotTerminated(f"Resolver chain from {start_kind!r} exceeds depth cap {depth_cap}")

            _dfs(target, start_kind, visited | {target}, next_depth)

    for kind in resolver_kinds:
        _dfs(kind, kind, frozenset({kind}), 0)


__all__ = ["register_ready_adapters", "validate_chain_closure"]
