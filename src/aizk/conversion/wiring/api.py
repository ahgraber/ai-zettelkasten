"""API runtime builder — assembles the submission-facing capability descriptor."""

from __future__ import annotations

from dataclasses import dataclass

from aizk.conversion.core.errors import ConfigurationError
from aizk.conversion.core.registry import FetcherRegistry
from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig
from aizk.conversion.wiring.capabilities import SubmissionCapabilities
from aizk.conversion.wiring.ingress_policy import IngressPolicy
from aizk.conversion.wiring.registrations import register_fetchers


@dataclass
class ApiRuntime:
    """Assembled API-side runtime: submission capability descriptor."""

    capabilities: SubmissionCapabilities
    docling_config: DoclingConverterConfig


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
    from dotenv import load_dotenv

    load_dotenv()
    if ingress_policy is None:
        ingress_policy = IngressPolicy()

    fetcher_registry = FetcherRegistry()
    docling_config = DoclingConverterConfig()
    karakeep_cfg = KarakeepFetcherConfig()
    register_fetchers(fetcher_registry, cfg, karakeep_cfg=karakeep_cfg)

    registered = fetcher_registry.registered_kinds()
    unknown = ingress_policy.accepted_submission_kinds - registered
    if unknown:
        raise ConfigurationError(f"IngressPolicy references kinds not registered: {sorted(unknown)}")

    capabilities = SubmissionCapabilities(ingress_policy.accepted_submission_kinds)
    return ApiRuntime(capabilities=capabilities, docling_config=docling_config)


__all__ = ["ApiRuntime", "build_api_runtime"]
