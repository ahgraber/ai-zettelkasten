"""Worker polling loop and stale job recovery."""

from __future__ import annotations

import datetime as dt
import logging
import time

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlmodel import Session, select

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.db import get_engine
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.workers.orchestrator import process_job_supervised
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


def poll_and_process_jobs(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> bool:
    """Pick up the next eligible job and invoke supervised processing."""
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
            return False
        except DBAPIError:
            session.rollback()
            logger.exception("Job poll failed due to database error")
            return False

        if not job:
            session.rollback()
            return False

        job_id = job.id
        job.status = ConversionJobStatus.RUNNING
        job.started_at = now
        job.attempts += 1
        job.updated_at = now
        session.add(job)
        session.commit()

    if job_id is None:
        raise RuntimeError("Queued job missing id; cannot process job")

    process_job_supervised(job_id, config, poll_interval_seconds=poll_interval_seconds)
    return True


def run_worker(config: ConversionConfig, poll_interval_seconds: float = 2.0) -> int:
    """Run the worker loop for polling, processing, and recovery.

    Returns an exit code: 0 for clean shutdown, 1 for forced termination.
    """
    register_signal_handlers()
    logger.info(
        "Starting conversion worker loop (drain_timeout=%ds)",
        config.worker_drain_timeout_seconds,
    )

    last_recovery_check = 0.0
    while not is_shutdown_requested():
        now = time.monotonic()
        if now - last_recovery_check >= config.worker_stale_job_check_seconds:
            recovered = recover_stale_running_jobs(config)
            if recovered:
                logger.warning("Recovered %d stale RUNNING jobs", recovered)
            last_recovery_check = now

        processed = poll_and_process_jobs(config, poll_interval_seconds=poll_interval_seconds)

        # If shutdown was requested during job processing, the supervision
        # loop already handled the drain (waiting for completion or
        # force-terminating after drain timeout).  Log and exit.
        if is_shutdown_requested():
            if processed:
                logger.info("Shutdown completed — in-flight job finished during drain")
            else:
                logger.info("Shutdown requested — exiting")
            if is_immediate_shutdown():
                logger.warning("Forced shutdown — exiting with code 1")
                return 1
            return 0

        if not processed:
            time.sleep(poll_interval_seconds)

    # Shutdown while idle (signal arrived during sleep).
    logger.info("Shutdown requested while idle — exiting cleanly")
    return 0
