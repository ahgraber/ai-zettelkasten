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


def build_api_runtime(cfg: object) -> ApiRuntime:
    """Build the API-process runtime.

    Registers the same fetcher adapters as the worker runtime so chain-closure
    validation covers all resolver edges.  The API gate then narrows
    ``accepted_kinds`` to the subset declared ``api_submittable`` by each
    adapter — worker-internal resolver targets (arxiv, url, etc.) declare
    ``api_submittable = False`` so they cannot be directly submitted.

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

    capabilities = DeploymentCapabilities(
        accepted_kinds=fetcher_registry.submittable_kinds(),
        content_type_map=content_type_map,
        registered_content_types=registered_content_types,
        startup_probes=[],
    )

    return ApiRuntime(
        capabilities=capabilities,
        fetcher_registry=fetcher_registry,
        converter_registry=converter_registry,
    )
