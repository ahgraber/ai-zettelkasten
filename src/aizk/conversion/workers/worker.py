"""Conversion worker entrypoint: drives the stage through the pipeline runner.

The CLI ``worker`` command runs this. It builds a
:class:`~aizk.conversion.handler.ConversionStageHandler` and drives it
through the generic :class:`~aizk.pipeline.runner.StageRunner`, mapping the
conversion service's configured loop timings onto the runner:

* ``worker_concurrency`` and ``worker_job_timeout_seconds`` are owned by the
  handler and read by the runner through its protocol.
* ``worker_drain_timeout_seconds`` -> ``drain_timeout``.
* ``worker_stale_job_check_seconds`` -> ``stale_recovery_interval``.
* ``cancel_grace`` covers conversion's subprocess termination budget
  (see ``_SUBPROCESS_TERMINATION_BUDGET_SECONDS``).
"""

from __future__ import annotations

import logging

from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.utilities.config import ConversionConfig
from aizk.pipeline.runner import StageRunner
from aizk.pipeline.shutdown import ShutdownController

logger = logging.getLogger(__name__)

# Conversion terminates a subprocess by SIGTERM, waiting 5s, then SIGKILL,
# waiting 5s more (``supervision._terminate_and_wait``). On the drain-timeout
# survivor path the runner drives that termination via ``cancel`` and waits
# ``cancel_grace`` for it to complete, so the grace must cover the full 10s
# window — otherwise a subprocess still awaiting SIGKILL is reported as a forced
# exit before its termination has had its full budget.
_SUBPROCESS_TERMINATION_BUDGET_SECONDS = 10.0


def run_worker(
    config: ConversionConfig,
    *,
    shutdown: ShutdownController | None = None,
) -> int:
    """Run the conversion worker via the pipeline :class:`StageRunner`.

    Builds the worker runtime, a
    :class:`~aizk.conversion.handler.ConversionStageHandler`, and a
    :class:`~aizk.pipeline.runner.StageRunner` with the loop timings mapped
    from ``config`` (see this module's docstring), then runs the supervised
    loop. The runner installs the signal handlers, validates dependencies,
    advertises the stage role in the process title, drives the
    claim/concurrency/drain/timeout/stale-recovery loop, and returns the exit
    code. Concurrency (``worker_concurrency``) and the per-job wall-clock
    timeout (``worker_job_timeout_seconds``) are owned by the handler and
    read by the runner through its protocol.

    Args:
        config: The conversion service configuration, supplying the loop timings
            and the handler's concurrency/timeout.
        shutdown: Optional shutdown controller. The CLI omits it (the runner
            creates one and installs OS signal handlers in ``run``); a
            programmatic driver (tests, embedding) may pass its own so it can
            :meth:`~aizk.pipeline.shutdown.ShutdownController.request_shutdown`
            without delivering a process signal.

    Returns:
        The runner exit code: ``0`` for a clean drain, ``1`` for a forced exit.
    """
    from aizk.conversion.wiring.worker import build_worker_runtime

    runtime = build_worker_runtime(config)
    handler = ConversionStageHandler(config, runtime=runtime)
    runner = StageRunner(
        handler,
        engine=_engine_for(config),
        shutdown=shutdown,
        drain_timeout=float(config.worker_drain_timeout_seconds),
        stale_recovery_interval=config.worker_stale_job_check_seconds,
        cancel_grace=_SUBPROCESS_TERMINATION_BUDGET_SECONDS,
    )
    logger.info(
        "Starting conversion worker (concurrency=%d, gpu_concurrency=%d, drain_timeout=%ds, stale_check=%ss)",
        config.worker_concurrency,
        config.worker_gpu_concurrency,
        config.worker_drain_timeout_seconds,
        config.worker_stale_job_check_seconds,
    )
    return runner.run()


def _engine_for(config: ConversionConfig):
    """Return the SQLite engine the runner opens its transactions on.

    The runner owns the ``BEGIN IMMEDIATE`` transactions for claim / finalize /
    recover; it needs the same engine the handler's per-call helpers build
    from ``config.database_url``. Built here (not lazily inside the handler)
    because the runner constructor requires the engine up front.
    """
    from aizk.conversion.db import get_engine

    return get_engine(config.database_url)


__all__ = ["run_worker"]
