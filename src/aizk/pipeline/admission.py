"""Admit pending work into a stage's queue within its capacity.

:class:`AdmissionAdapter` declares one stage's admission: its pending-work
selection, its enqueue, its capacity, and whether automatic admission runs.
:func:`run_admission_pass` performs one bounded write transaction through an
adapter and leaves omitted work pending. :class:`AdmissionLoop` runs the pass
in a worker thread at a fixed interval. :class:`AdmissionConfig` is the
settings section a stage's adapter is built from.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from aizk.db.engine import begin_immediate
from aizk.pipeline.capacity import StageAtCapacityError, headroom
from aizk.pipeline.lifecycle import MAX_BULK_SELECTION

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine
    from sqlmodel import Session

logger = logging.getLogger(__name__)


class AdmissionConfig(BaseSettings):
    """Admission and queue-capacity settings for graph stages.

    Read from ``AIZK_GRAPH__*``. Each stage has its own queue limit and admission
    flag; a non-positive queue limit disables the limit. The
    ``queue_retry_after_seconds`` field sets the intake ``Retry-After`` value.
    Numeric settings reject invalid values at construction.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIZK_GRAPH__",
        env_file=None,
        extra="ignore",
    )

    contextualization_queue_max_depth: int = Field(default=0, ge=0)
    extraction_queue_max_depth: int = Field(default=0, ge=0)
    queue_retry_after_seconds: int = Field(default=30, ge=1)

    admission_contextualization_enabled: bool = False
    admission_extraction_enabled: bool = False
    admission_interval_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)


@dataclass(frozen=True)
class AdmissionAdapter:
    """Configuration for admitting one stage's pending work.

    Attributes:
        stage: Stage name.
        model: Work-unit model used to measure capacity.
        pending_work: Function that returns pending keys up to a limit.
        enqueue: Function that creates or reuses a work-unit.
        queue_max_depth: Capacity limit; ``0`` disables the limit.
        enabled: Whether automatic admission runs.
    """

    stage: str
    model: Any
    pending_work: "Callable[[Session, int | None], list[Any]]"
    enqueue: "Callable[[Session, Any, int, str], tuple[Any, bool]]"
    queue_max_depth: int
    enabled: bool


def run_admission_pass(engine: "Engine", adapter: AdmissionAdapter) -> int:
    """Admit pending work through ``adapter`` in one short write transaction.

    A disabled or full stage creates no units. The pass creates at most
    ``MAX_BULK_SELECTION`` units; omitted work remains pending.

    Returns:
        The number of work-units created.
    """
    if not adapter.enabled:
        return 0

    admitted = 0
    principal = f"worker:{os.getpid()}"
    with begin_immediate(engine) as session:
        room = headroom(session, adapter.model, limit=adapter.queue_max_depth)
        if room == 0:
            logger.info(
                "Admission for %s is at capacity; nothing admitted this pass",
                adapter.stage,
                extra={"stage": adapter.stage, "limit": adapter.queue_max_depth},
            )
            return 0
        batch = MAX_BULK_SELECTION if room is None else min(room, MAX_BULK_SELECTION)
        pending = adapter.pending_work(session, batch)
        for key in pending:
            try:
                _unit, created = adapter.enqueue(session, key, adapter.queue_max_depth, principal)
            except StageAtCapacityError:
                # A concurrent writer took the room this pass measured. The
                # remainder is still pending; the next pass admits it.
                logger.info(
                    "Admission for %s stopped at capacity after %d unit(s)",
                    adapter.stage,
                    admitted,
                    extra={"stage": adapter.stage, "admitted": admitted},
                )
                break
            if created:
                admitted += 1
    if admitted:
        logger.info(
            "Admitted %d %s work-unit(s)",
            admitted,
            adapter.stage,
            extra={"stage": adapter.stage, "admitted": admitted},
        )
    return admitted


class AdmissionLoop:
    """Run a stage's admission pass on an interval in a worker process.

    The loop owns one background thread. ``stop`` signals and joins it; pass errors
    are logged and the loop continues.
    """

    def __init__(
        self,
        engine: "Engine",
        adapter: AdmissionAdapter,
        *,
        interval_seconds: float,
    ) -> None:
        """Bind the loop to an engine, a stage's declaration, and its tick interval."""
        self._engine = engine
        self._adapter = adapter
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "AdmissionLoop":
        """Start the loop on entering the ``with`` block."""
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Stop and join the loop on leaving the ``with`` block."""
        self.stop()

    def start(self) -> None:
        """Start the background thread, running a first pass immediately."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"admission-{self._adapter.stage}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float | None = None) -> None:
        """Signal the loop to finish its current pass and wait for the thread to exit."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self) -> None:
        """Run a pass, then wait one interval, until stopped."""
        logger.info(
            "Starting admission loop for %s (enabled=%s interval=%.1fs)",
            self._adapter.stage,
            self._adapter.enabled,
            self._interval,
            extra={"stage": self._adapter.stage, "enabled": self._adapter.enabled},
        )
        while not self._stop.is_set():
            try:
                run_admission_pass(self._engine, self._adapter)
            except Exception:
                logger.exception(
                    "Admission pass for %s failed; retrying next interval",
                    self._adapter.stage,
                    extra={"stage": self._adapter.stage},
                )
            self._stop.wait(self._interval)
        logger.info("Admission loop for %s stopped", self._adapter.stage, extra={"stage": self._adapter.stage})


__all__ = [
    "MAX_BULK_SELECTION",
    "AdmissionAdapter",
    "AdmissionConfig",
    "AdmissionLoop",
    "run_admission_pass",
]
