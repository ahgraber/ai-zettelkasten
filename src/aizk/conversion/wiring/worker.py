"""Worker runtime builder — assembles the full fetch/convert pipeline for worker processes."""

from __future__ import annotations

from dataclasses import dataclass
import threading

from aizk.conversion.core.orchestrator import Orchestrator
from aizk.conversion.core.protocols import ResourceGuard
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.types import ContentType
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.wiring.capabilities import DeploymentCapabilities
from aizk.conversion.wiring.registrations import register_ready_adapters


class _SemaphoreGuard:
    """Wraps a ``threading.Semaphore`` as a ``ResourceGuard`` context manager."""

    def __init__(self, semaphore: threading.Semaphore) -> None:
        self._semaphore = semaphore

    def __enter__(self) -> "_SemaphoreGuard":
        self._semaphore.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._semaphore.release()


@dataclass
class WorkerRuntime:
    """Assembled worker-side runtime: orchestrator, resource guard, and capabilities."""

    orchestrator: Orchestrator
    resource_guard: ResourceGuard
    capabilities: DeploymentCapabilities


def build_worker_runtime(cfg: ConversionConfig) -> WorkerRuntime:
    """Build and return a fully wired ``WorkerRuntime``.

    Populates fresh registries, registers all production-ready adapters,
    validates the resolver chain, and wires the Orchestrator with DI callables.

    Args:
        cfg: Conversion configuration forwarded to adapters that require it.

    Returns:
        A ``WorkerRuntime`` ready for use in a worker process.
    """
    fetcher_registry = FetcherRegistry()
    converter_registry = ConverterRegistry()
    register_ready_adapters(fetcher_registry, converter_registry, cfg)

    semaphore = threading.Semaphore(cfg.worker_gpu_concurrency)
    resource_guard: ResourceGuard = _SemaphoreGuard(semaphore)

    def resolve_fetcher(kind: str):
        return fetcher_registry.resolve(kind)

    def resolve_converter(content_type: ContentType, name: str):
        return converter_registry.resolve(content_type, name)

    orchestrator = Orchestrator(
        resolve_fetcher=resolve_fetcher,
        resolve_converter=resolve_converter,
    )
    capabilities = DeploymentCapabilities(fetcher_registry, converter_registry)

    return WorkerRuntime(
        orchestrator=orchestrator,
        resource_guard=resource_guard,
        capabilities=capabilities,
    )


__all__ = ["WorkerRuntime", "build_worker_runtime"]
