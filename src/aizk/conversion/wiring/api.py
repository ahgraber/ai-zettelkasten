"""API runtime builder — assembles the submission-facing capability descriptor."""

from __future__ import annotations

from dataclasses import dataclass

from aizk.conversion.adapters.converters.docling import DoclingConverter
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
    converter_name: str
    converter_config_snapshot: dict[str, object]
    docling_config: DoclingConverterConfig
    docling_config_snapshot: dict[str, object]


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
        ingress_policy = IngressPolicy()

    fetcher_registry = FetcherRegistry()
    docling_config = DoclingConverterConfig()
    docling_snapshot = DoclingConverter(docling_config).config_snapshot()
    karakeep_cfg = KarakeepFetcherConfig()
    register_fetchers(fetcher_registry, cfg, karakeep_cfg=karakeep_cfg)

    registered = fetcher_registry.registered_kinds()
    unknown = ingress_policy.accepted_submission_kinds - registered
    if unknown:
        raise ConfigurationError(f"IngressPolicy references kinds not registered: {sorted(unknown)}")

    converter_name = cfg.worker_converter_name
    if converter_name != "docling":
        raise ConfigurationError(
            f"API runtime cannot build submission config snapshot for converter {converter_name!r}"
        )

    capabilities = SubmissionCapabilities(ingress_policy.accepted_submission_kinds)
    return ApiRuntime(
        capabilities=capabilities,
        converter_name=converter_name,
        converter_config_snapshot=docling_snapshot,
        docling_config=docling_config,
        docling_config_snapshot=docling_snapshot,
    )


__all__ = ["ApiRuntime", "build_api_runtime"]
