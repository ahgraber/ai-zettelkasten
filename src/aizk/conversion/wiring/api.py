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


def build_api_runtime(cfg: object) -> ApiRuntime:
    """Build the API-process runtime.

    Registers the same adapters as the worker runtime (via the shared
    ``register_ready_adapters`` helper) so ``accepted_kinds`` cannot drift
    between the two process roles.  The API does not need converter adapters
    at call time, but registering them ensures capability parity.

    Args:
        cfg: ``ConversionConfig`` (or compatible) instance.

    Returns:
        An ``ApiRuntime`` with deployment capabilities for request validation.
    """
    fetcher_registry = FetcherRegistry()
    converter_registry = ConverterRegistry()

    content_type_map, registered_content_types = register_ready_adapters(
        fetcher_registry, converter_registry, cfg
    )

    capabilities = DeploymentCapabilities(
        accepted_kinds=fetcher_registry.registered_kinds(),
        content_type_map=content_type_map,
        registered_content_types=registered_content_types,
        startup_probes=[],
    )

    return ApiRuntime(
        capabilities=capabilities,
        fetcher_registry=fetcher_registry,
        converter_registry=converter_registry,
    )
