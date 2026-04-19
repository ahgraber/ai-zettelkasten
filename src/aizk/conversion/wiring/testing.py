"""Test runtime builder — provides a test-configurable runtime for unit and integration tests."""

from __future__ import annotations

from dataclasses import dataclass

from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.wiring.capabilities import DeploymentCapabilities
from aizk.conversion.wiring.ingress_policy import IngressPolicy


@dataclass
class TestRuntime:
    """Test-configurable runtime exposing registries directly for fake injection."""

    fetcher_registry: FetcherRegistry
    converter_registry: ConverterRegistry
    capabilities: DeploymentCapabilities
    ingress_policy: IngressPolicy


def build_test_runtime(
    cfg: ConversionConfig,
    *,
    ingress_policy: IngressPolicy | None = None,
) -> TestRuntime:
    """Build a test-configurable runtime with real or fake adapters.

    Returns empty registries — tests may register their own fakes instead of
    calling ``register_ready_adapters``.

    Args:
        cfg: Conversion configuration (not used to populate registries here).
        ingress_policy: Ingress policy to use. If ``None``, defaults to
            ``{"karakeep_bookmark"}`` with no ``.env`` file loaded.

    Returns:
        A ``TestRuntime`` with empty registries and a ``DeploymentCapabilities``
        descriptor reflecting whatever adapters the test registers.
    """
    if ingress_policy is None:
        ingress_policy = IngressPolicy(
            _env_file=None,
            accepted_submission_kinds=frozenset({"karakeep_bookmark"}),
        )

    fetcher_registry = FetcherRegistry()
    converter_registry = ConverterRegistry()
    # NOTE: Tests may register their own fakes instead of calling register_ready_adapters
    capabilities = DeploymentCapabilities(fetcher_registry, converter_registry)

    return TestRuntime(
        fetcher_registry=fetcher_registry,
        converter_registry=converter_registry,
        capabilities=capabilities,
        ingress_policy=ingress_policy,
    )


__all__ = ["TestRuntime", "build_test_runtime"]
