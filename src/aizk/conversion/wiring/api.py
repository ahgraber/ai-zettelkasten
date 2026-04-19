"""API runtime builder — assembles the submission-facing capability descriptor."""

from __future__ import annotations

from dataclasses import dataclass

from aizk.conversion.core.errors import ConfigurationError
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.wiring.capabilities import SubmissionCapabilities
from aizk.conversion.wiring.ingress_policy import IngressPolicy
from aizk.conversion.wiring.registrations import register_ready_adapters


@dataclass
class ApiRuntime:
    """Assembled API-side runtime: submission capability descriptor."""

    capabilities: SubmissionCapabilities


def build_api_runtime(
    cfg: ConversionConfig,
    *,
    ingress_policy: IngressPolicy | None = None,
) -> ApiRuntime:
    """Build and return a fully wired ``ApiRuntime``.

    Validates that every kind in ``ingress_policy.accepted_submission_kinds`` is
    registered in the fetcher registry. Raises ``ConfigurationError`` on
    mismatch.

    Args:
        cfg: Conversion configuration forwarded to adapters that require it.
        ingress_policy: Ingress policy to use. If ``None``, reads from the
            environment (no ``.env`` file loaded).

    Returns:
        An ``ApiRuntime`` with a ``SubmissionCapabilities`` descriptor.

    Raises:
        ConfigurationError: If the ingress policy references an unregistered kind.
    """
    if ingress_policy is None:
        ingress_policy = IngressPolicy(_env_file=None)

    fetcher_registry = FetcherRegistry()
    converter_registry = ConverterRegistry()
    register_ready_adapters(fetcher_registry, converter_registry, cfg)

    registered = fetcher_registry.registered_kinds()
    unknown = ingress_policy.accepted_submission_kinds - registered
    if unknown:
        raise ConfigurationError(f"IngressPolicy references kinds not registered: {sorted(unknown)}")

    capabilities = SubmissionCapabilities(ingress_policy.accepted_submission_kinds)
    return ApiRuntime(capabilities=capabilities)


__all__ = ["ApiRuntime", "build_api_runtime"]
