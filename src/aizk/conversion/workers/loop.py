"""Worker polling loop and stale job recovery."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
import datetime as dt
import logging
import os
import time

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlmodel import Session, select

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers.orchestrator import configure_gpu_semaphore, process_job_supervised
from aizk.conversion.workers.shutdown import (
    is_immediate_shutdown,
    is_shutdown_requested,
    register_signal_handlers,
)
from aizk.conversion.workers.types import _utcnow

logger = logging.getLogger(__name__)


def recover_stale_running_jobs(config: ConversionConfig) -> int:
    """Mark stale RUNNING jobs as retryable.

    This can catch jobs that were being processed when a worker crashed.
    """
    engine = get_engine(config.database_url)
    now = _utcnow()
    stale_before = now - dt.timedelta(minutes=config.worker_stale_job_minutes)

    with Session(engine) as session:
        jobs = session.exec(
            select(ConversionJob)
            .where(ConversionJob.status == ConversionJobStatus.RUNNING)
            .where(ConversionJob.started_at.is_not(None))  # type: ignore[operator]
            .where(ConversionJob.started_at < stale_before)
        ).all()

        if not jobs:
            return 0

        for job in jobs:
            job.status = ConversionJobStatus.FAILED_RETRYABLE
            job.earliest_next_attempt_at = now
            job.error_code = "worker_stale_running"
            job.error_message = f"Marked stale after {config.worker_stale_job_minutes} minutes without completion."
            job.last_error_at = now
            job.updated_at = now
            session.add(job)

        session.commit()

    return len(jobs)


def claim_next_job(config: ConversionConfig) -> int | None:
    """Atomically claim the next eligible job and transition it to RUNNING.

    Returns the job id, or None if no eligible job exists or the database
    is locked.
    """
    engine = get_engine(config.database_url)
    now = _utcnow()

    with Session(engine) as session:
        try:
            # BEGIN IMMEDIATE prevents multiple workers from selecting the same job.
            session.exec(text("BEGIN IMMEDIATE"))
            job = session.exec(
                select(ConversionJob)
                .where(ConversionJob.status.in_([ConversionJobStatus.QUEUED, ConversionJobStatus.FAILED_RETRYABLE]))
                .where(
                    (ConversionJob.earliest_next_attempt_at.is_(None))  # type: ignore[operator]
                    | (ConversionJob.earliest_next_attempt_at <= now)
                )
                .order_by(ConversionJob.queued_at)
            ).first()
        except OperationalError as exc:
            session.rollback()
            logger.warning("Job poll skipped due to database lock: %s", exc)
            return None
        except DBAPIError:
            session.rollback()
            logger.exception("Job poll failed due to database error")
            return None

        if not job:
            session.rollback()
            return None

        job_id = job.id
        job.status = ConversionJobStatus.RUNNING
        job.started_at = now
        job.attempts += 1
        job.updated_at = now
        session.add(job)
        session.commit()

    if job_id is None:
        raise RuntimeError("Queued job missing id; cannot process job")

    return job_id


def poll_and_process_jobs(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> bool:
    """Pick up the next eligible job and invoke supervised processing."""
    job_id = claim_next_job(config)
    if job_id is None:
        return False
    process_job_supervised(job_id, config, poll_interval_seconds=poll_interval_seconds)
    return True


def _reap_completed(futures: dict[Future, int]) -> None:
    """Remove completed futures and log any unexpected exceptions."""
    done = [f for f in futures if f.done()]
    for f in done:
        job_id = futures.pop(f)
        exc = f.exception()
        if exc is not None:
            logger.error("Job %d raised unexpected exception: %s", job_id, exc)


def _drain_in_flight(futures: dict[Future, int], config: ConversionConfig) -> bool:
    """Wait for in-flight jobs during shutdown.

    Returns True if any job did not complete within the drain window
    (exit code should be 1).
    """
    if not futures:
        return False

    # Per-job supervision loops enforce their own drain deadlines.
    # Add a 15-second buffer so the outer wait outlasts them.
    outer_timeout = config.worker_drain_timeout_seconds + 15.0
    done, not_done = wait(futures.keys(), timeout=outer_timeout)

    if not_done:
        logger.warning(
            "Drain timeout expired with %d jobs still running",
            len(not_done),
        )
        return True

    # Check for exceptions in completed futures.
    for f in done:
        exc = f.exception()
        if exc is not None:
            logger.error("Job %d raised unexpected exception during drain: %s", futures[f], exc)

    return False


def run_worker(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> int:
    """Run the worker loop for polling, processing, and recovery.

    Returns an exit code: 0 for clean shutdown, 1 for forced termination.
    """
    register_signal_handlers()
    configure_gpu_semaphore(config.worker_gpu_concurrency)

    max_workers = config.worker_concurrency
    logger.info(
        "Starting conversion worker loop (concurrency=%d, gpu_concurrency=%d, drain_timeout=%ds)",
        max_workers,
        config.worker_gpu_concurrency,
        config.worker_drain_timeout_seconds,
    )

    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures: dict[Future, int] = {}
    last_recovery_check = 0.0
    force_terminated = False

    try:
        while not is_shutdown_requested():
            now = time.monotonic()
            if now - last_recovery_check >= config.worker_stale_job_check_seconds:
                recovered = recover_stale_running_jobs(config)
                if recovered:
                    logger.warning("Recovered %d stale RUNNING jobs", recovered)
                last_recovery_check = now

            _reap_completed(futures)

            # Fill worker slots greedily.
            if len(futures) < max_workers:
                job_id = claim_next_job(config)
                if job_id is not None:
                    future = executor.submit(
                        process_job_supervised,
                        job_id,
                        config,
                        poll_interval_seconds=poll_interval_seconds,
                    )
                    futures[future] = job_id
                    continue  # Try to fill more slots immediately.

            time.sleep(poll_interval_seconds)

        # Shutdown requested — drain in-flight jobs.
        logger.info("Shutdown requested — draining %d in-flight jobs", len(futures))
        force_terminated = _drain_in_flight(futures, config)

    finally:
        executor.shutdown(wait=False)

    if is_immediate_shutdown() or force_terminated:
        logger.warning("Forced shutdown — exiting with code 1")
        # ThreadPoolExecutor threads are not daemon threads.  A normal
        # return / sys.exit still waits for them during interpreter
        # shutdown, so a stuck task would keep the process alive
        # indefinitely.  os._exit bypasses that join.
        os._exit(1)

    logger.info("Shutdown complete — exiting cleanly")
    return 0
