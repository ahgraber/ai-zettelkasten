"""Worker runtime builder — assembles the full fetch/convert pipeline for worker processes."""

from __future__ import annotations

from dataclasses import dataclass
import threading

from aizk.conversion.core.coordinator import ConversionCoordinator
from aizk.conversion.core.protocols import ResourceGuard
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.types import ContentType
from aizk.conversion.settings import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig
from aizk.conversion.wiring.capabilities import DeploymentCapabilities
from aizk.conversion.wiring.registrations import build_startup_probes, register_ready_adapters
from aizk.storage.settings import ObjectStorageSettings


class _SemaphoreGuard:
    """Wraps a ``threading.Semaphore`` as a ``ResourceGuard`` context manager."""

    def __init__(self, semaphore: threading.BoundedSemaphore) -> None:
        self._semaphore = semaphore

    def __enter__(self) -> "_SemaphoreGuard":
        self._semaphore.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._semaphore.release()


@dataclass
class WorkerRuntime:
    """Assembled worker-side runtime: coordinator, resource guard, capabilities, and object storage."""

    coordinator: ConversionCoordinator
    resource_guard: ResourceGuard
    capabilities: DeploymentCapabilities
    object_storage: ObjectStorageSettings


def build_worker_runtime(cfg: ConversionConfig, *, object_storage: ObjectStorageSettings) -> WorkerRuntime:
    """Build and return a fully wired ``WorkerRuntime``.

    Populates fresh registries, registers all production-ready adapters,
    validates the resolver chain, and wires the ConversionCoordinator with DI callables.

    Args:
        cfg: Conversion configuration forwarded to adapters that require it.
        object_storage: Object-storage settings for the S3 startup probe and
            the assembled runtime.

    Returns:
        A ``WorkerRuntime`` ready for use in a worker process.
    """
    fetcher_registry = FetcherRegistry()
    converter_registry = ConverterRegistry()
    docling_cfg = DoclingConverterConfig()
    karakeep_cfg = KarakeepFetcherConfig()
    register_ready_adapters(
        fetcher_registry, converter_registry, cfg, docling_cfg=docling_cfg, karakeep_cfg=karakeep_cfg
    )

    semaphore = threading.BoundedSemaphore(cfg.worker_gpu_concurrency)
    resource_guard: ResourceGuard = _SemaphoreGuard(semaphore)

    def resolve_fetcher(kind: str):
        return fetcher_registry.resolve(kind)

    def resolve_converter(content_type: ContentType, name: str):
        return converter_registry.resolve(content_type, name)

    coordinator = ConversionCoordinator(
        resolve_fetcher=resolve_fetcher,
        resolve_converter=resolve_converter,
    )
    startup_probes = build_startup_probes(
        fetcher_registry, cfg, docling_cfg, karakeep_cfg, object_storage=object_storage
    )
    capabilities = DeploymentCapabilities(fetcher_registry, converter_registry, startup_probes=startup_probes)

    return WorkerRuntime(
        coordinator=coordinator,
        resource_guard=resource_guard,
        capabilities=capabilities,
        object_storage=object_storage,
    )


__all__ = ["WorkerRuntime", "build_worker_runtime"]
