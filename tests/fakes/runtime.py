"""A conversion worker runtime whose collaborators are mocks."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aizk.conversion.wiring.worker import WorkerRuntime


def fake_worker_runtime() -> "WorkerRuntime":
    """A runtime whose converter never requires the GPU guard."""
    from contextlib import nullcontext
    from unittest.mock import MagicMock

    from aizk.conversion.wiring.subprocess import _spawn_conversion_subprocess
    from aizk.conversion.wiring.worker import WorkerRuntime
    from aizk.storage.settings import ObjectStorageSettings

    capabilities = MagicMock()
    capabilities.converter_requires_gpu.return_value = False
    return WorkerRuntime(
        coordinator=MagicMock(),
        resource_guard=nullcontext(),
        capabilities=capabilities,
        object_storage=ObjectStorageSettings(_env_file=None),
        spawn_subprocess=_spawn_conversion_subprocess,
    )
