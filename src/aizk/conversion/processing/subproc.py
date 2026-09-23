"""Per-job conversion subprocess: supervise, cancellation, and IPC."""

from __future__ import annotations

from collections.abc import Callable
import multiprocessing as mp
from pathlib import Path
import time
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine
from sqlmodel import Session

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.processing.supervision import _supervise_conversion_process
from aizk.conversion.processing.types import SupervisionResult
from aizk.conversion.settings import ConversionConfig

if TYPE_CHECKING:
    import threading


def _is_job_cancelled(job_id: int, engine: Engine) -> bool:
    """Return True when the job status is CANCELLED."""
    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        return bool(job and job.status == ConversionJobStatus.CANCELLED)


def _spawn_and_supervise(
    *,
    job_id: int,
    workspace: Path,
    source_ref_json: str,
    poll_interval_seconds: float,
    timeout_seconds: float,
    is_cancelled_fn: Callable[[], bool],
    config: ConversionConfig,
    resource_guard,
    requires_gpu: bool,
    spawn_subprocess: Callable[..., tuple[mp.Process, mp.Queue]],
    on_phase_event=None,
    on_spawn: Callable[[mp.Process], None] | None = None,
    terminate_event: "threading.Event | None" = None,
    shutdown_requested_fn: Callable[[], bool] | None = None,
) -> tuple[mp.Process, SupervisionResult, float | None]:
    """Spawn and supervise; acquire resource_guard only if requires_gpu.

    ``on_spawn``, when supplied, is invoked once with the freshly spawned
    subprocess immediately before supervision begins blocking. The pipeline
    runner adapter uses it to register the live process so its cancellation
    hook can terminate the process group while supervision is still running.

    ``terminate_event``, when supplied, is forwarded to the supervision loop so
    an out-of-band owner can *signal* termination (the supervision loop, the
    single owner of the Process, performs the actual terminate/join). The
    pipeline runner adapter uses this seam to drive drain/cancel.

    ``shutdown_requested_fn`` defaults to ``None``: the runner owns drain (it
    cancels in-flight units via ``terminate_event`` on shutdown), so the
    supervision loop's module-global shutdown-drain branch is dead on the
    runner path and the adapter passes ``None``. The parameter exists so the
    supervision loop never depends on any module-global shutdown state.
    """
    from contextlib import nullcontext

    guard_ctx = resource_guard if (requires_gpu and resource_guard is not None) else nullcontext()

    with guard_ctx:
        process, status_queue = spawn_subprocess(
            job_id=job_id,
            workspace=workspace,
            source_ref_json=source_ref_json,
        )

        if on_spawn is not None:
            on_spawn(process)

        deadline = None
        if timeout_seconds > 0:
            deadline = time.monotonic() + timeout_seconds

        result = _supervise_conversion_process(
            job_id=job_id,
            process=process,
            status_queue=status_queue,
            poll_interval_seconds=poll_interval_seconds,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            is_cancelled_fn=is_cancelled_fn,
            shutdown_requested_fn=shutdown_requested_fn,
            drain_timeout_seconds=float(config.worker_drain_timeout_seconds),
            on_phase_event=on_phase_event,
            terminate_event=terminate_event,
        )
    return process, result, deadline


def __getattr__(name: str):
    """Serve the child-entry names from ``aizk.conversion.wiring.subprocess`` on first access."""
    if name in ("_raise_if_cancelled", "_report_status", "_process_job_subprocess", "_spawn_conversion_subprocess"):
        from aizk.conversion.wiring import subprocess as wiring_subprocess

        return getattr(wiring_subprocess, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
