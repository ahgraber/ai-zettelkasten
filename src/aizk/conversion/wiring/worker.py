"""Worker runtime builder for the conversion pipeline."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import TracebackType

from aizk.conversion.core.orchestrator import DEFAULT_DEPTH_CAP, Orchestrator
from aizk.conversion.core.protocols import ResourceGuard
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.wiring.capabilities import DeploymentCapabilities
from aizk.conversion.wiring.registrations import register_ready_adapters


class _SemaphoreGuard:
    """ResourceGuard backed by a threading.Semaphore."""

    def __init__(self, n: int = 1) -> None:
        self._sem = threading.Semaphore(n)

    def __enter__(self) -> _SemaphoreGuard:
        self._sem.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._sem.release()


@dataclass
class WorkerRuntime:
    """Assembled worker-process runtime."""

    orchestrator: Orchestrator
    gpu_guard: ResourceGuard
    capabilities: DeploymentCapabilities
    fetcher_registry: FetcherRegistry
    converter_registry: ConverterRegistry
    converter_name: str = "docling"

    def converter_requires_gpu(self, converter_name: str | None = None) -> bool:
        """Return ``requires_gpu`` for the named converter (default: this runtime's).

        ``requires_gpu`` is a class-level attribute on the Converter, so the
        same value applies regardless of content_type.  Returns False when the
        named converter is not registered (e.g. API-side runtimes).
        """
        name = converter_name or self.converter_name
        for (_content_type, reg_name), impl in self.converter_registry._entries.items():
            if reg_name == name:
                return bool(getattr(impl, "requires_gpu", False))
        return False


def build_worker_runtime(cfg: object) -> WorkerRuntime:
    """Build the full worker-process runtime.

    Registers all production-ready fetcher and converter adapters, validates
    the resolver chain, creates the GPU ``ResourceGuard``, and wires an
    ``Orchestrator``.

    Args:
        cfg: ``ConversionConfig`` (or compatible) instance.

    Returns:
        A ``WorkerRuntime`` with the assembled orchestrator, GPU guard, and
        deployment capabilities.
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

    gpu_concurrency = getattr(cfg, "worker_gpu_concurrency", 1)
    gpu_guard = _SemaphoreGuard(n=gpu_concurrency)

    orchestrator = Orchestrator(
        resolve_fetcher=fetcher_registry.resolve,
        resolve_converter=converter_registry.resolve,
        depth_cap=DEFAULT_DEPTH_CAP,
    )

    return WorkerRuntime(
        orchestrator=orchestrator,
        gpu_guard=gpu_guard,
        capabilities=capabilities,
        fetcher_registry=fetcher_registry,
        converter_registry=converter_registry,
    )
