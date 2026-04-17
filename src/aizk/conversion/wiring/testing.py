"""Test runtime builder for the conversion pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field

from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.wiring.capabilities import DeploymentCapabilities, Probe


@dataclass
class TestRuntime:
    """Assembled test-process runtime with in-memory registries."""

    fetcher_registry: FetcherRegistry
    converter_registry: ConverterRegistry
    capabilities: DeploymentCapabilities


def build_test_runtime(
    cfg: object = None,
    fetcher_registry: FetcherRegistry | None = None,
    converter_registry: ConverterRegistry | None = None,
    extra_probes: list[Probe] | None = None,
) -> TestRuntime:
    """Build a minimal runtime for testing with in-memory registries.

    Registries start empty; callers can register fake adapters after calling
    this function.  No real adapters are imported.

    Args:
        cfg: Optional config (unused in the default test runtime).
        fetcher_registry: Pre-configured registry, or a fresh empty one.
        converter_registry: Pre-configured registry, or a fresh empty one.
        extra_probes: Optional startup probes to include in capabilities.

    Returns:
        A ``TestRuntime`` with mutable registries and empty capabilities.
    """
    fr = fetcher_registry if fetcher_registry is not None else FetcherRegistry()
    cr = converter_registry if converter_registry is not None else ConverterRegistry()

    capabilities = DeploymentCapabilities(
        accepted_kinds=fr.registered_kinds(),
        content_type_map={},
        registered_content_types=frozenset(),
        startup_probes=extra_probes or [],
    )

    return TestRuntime(
        fetcher_registry=fr,
        converter_registry=cr,
        capabilities=capabilities,
    )
