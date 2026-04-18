"""FastAPI application setup for the conversion service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from aizk.conversion.api.routes import bookmarks_router, health_router, jobs_router, outputs_router, ui_router
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.logging import configure_logging
from aizk.conversion.wiring.api import build_api_runtime
from aizk.utilities.mlflow_tracing import configure_mlflow_tracing

logger = logging.getLogger(__name__)


async def _stale_job_reaper_loop(config: ConversionConfig) -> None:
    """Periodically reap RUNNING jobs whose worker died without reporting.

    F12/#33 — ``workers/loop.py::recover_stale_running_jobs`` already exists
    but only runs inside live worker processes. If every worker is down
    (OOM-killed, SIGKILL, os._exit) there's no one to reap. Piggyback on
    the API process, which is always up as long as ingress serves traffic,
    so zombie RUNNING rows get transitioned independently of worker health.

    The reap itself is idempotent and safe to run from multiple processes:
    ``recover_stale_running_jobs`` transitions RUNNING→FAILED_RETRYABLE in
    a single commit and targets only rows that have already crossed the
    ``worker_stale_job_minutes`` threshold.
    """
    # Late import to avoid pulling the worker runtime into API startup.
    from aizk.conversion.workers.loop import recover_stale_running_jobs

    interval = float(config.worker_stale_job_check_seconds)
    while True:
        try:
            await asyncio.sleep(interval)
            reaped = await asyncio.to_thread(recover_stale_running_jobs, config)
            if reaped:
                logger.warning(
                    "stale-job reaper recovered %d RUNNING job(s) (threshold=%d min)",
                    reaped,
                    config.worker_stale_job_minutes,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Never kill the reaper on a transient DB error — log and carry on.
            logger.exception("stale-job reaper iteration failed; continuing")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize resources needed for the API lifespan."""
    from aizk.conversion.migrations import run_migrations

    config = ConversionConfig()
    _app.state.config = config
    configure_logging(config)
    configure_mlflow_tracing(
        enabled=config.mlflow_tracing_enabled,
        tracking_uri=config.mlflow_tracking_uri,
        experiment_name=config.mlflow_experiment_name,
    )
    run_migrations()
    _app.state.api_runtime = build_api_runtime(config)

    reaper_task = asyncio.create_task(
        _stale_job_reaper_loop(config),
        name="stale_job_reaper",
    )
    _app.state.stale_job_reaper_task = reaper_task

    try:
        yield
    finally:
        reaper_task.cancel()
        try:
            await reaper_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def create_app() -> FastAPI:
    """Create the FastAPI application instance."""
    app = FastAPI(title="Docling Conversion Service", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(bookmarks_router)
    app.include_router(outputs_router)
    app.include_router(ui_router)

    @app.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        """Temporary shim redirecting root to the jobs UI."""
        return RedirectResponse(url="/ui/jobs", status_code=307)

    return app


app = create_app()
