"""API runtime builder for the conversion pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.wiring.capabilities import DeploymentCapabilities
from aizk.conversion.wiring.registrations import register_ready_adapters


@dataclass
class ApiRuntime:
    """Assembled API-process runtime."""

    capabilities: DeploymentCapabilities
    fetcher_registry: FetcherRegistry
    converter_registry: ConverterRegistry
    converter_name: str = "docling"


# Source-ref kinds the API endpoint accepts from callers.  Worker-internal
# resolver targets (arxiv, url, inline_html, github_readme) are registered for
# chain-closure validation but must not be directly submittable.
_API_SUBMITTABLE_KINDS: frozenset[str] = frozenset({"karakeep_bookmark"})


def build_api_runtime(cfg: object) -> ApiRuntime:
    """Build the API-process runtime.

    Registers the same fetcher adapters as the worker runtime so chain-closure
    validation covers all resolver edges.  The API gate then narrows
    ``accepted_kinds`` to only the directly-submittable kinds; worker-internal
    resolver targets (arxiv, url, etc.) are registered for chain-closure but
    must not be API-submittable.

    Args:
        cfg: ``ConversionConfig`` (or compatible) instance.

    Returns:
        An ``ApiRuntime`` with deployment capabilities for request validation.
    """
    fetcher_registry = FetcherRegistry()
    converter_registry = ConverterRegistry()

    # The API process doesn't run converters; skip DoclingConverter registration
    # (and its heavyweight imports) while keeping fetcher registration identical to
    # the worker so chain-closure validation covers all resolver edges.
    content_type_map, registered_content_types = register_ready_adapters(
        fetcher_registry, converter_registry, cfg, include_converters=False
    )

    # Fetchers are registered for all supported kinds (so chain_closure can validate them),
    # but the API gate only accepts directly-submitted kinds — worker-internal kinds like
    # "arxiv" and "inline_html" must not be submittable via the public endpoint.
    capabilities = DeploymentCapabilities(
        accepted_kinds=fetcher_registry.registered_kinds() & _API_SUBMITTABLE_KINDS,
        content_type_map=content_type_map,
        registered_content_types=registered_content_types,
        startup_probes=[],
    )

    return ApiRuntime(
        capabilities=capabilities,
        fetcher_registry=fetcher_registry,
        converter_registry=converter_registry,
    )
