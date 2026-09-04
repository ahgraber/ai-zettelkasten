"""Create graph-stage work-units from pending upstream state.

:class:`AdmissionAdapter` defines pending-work selection, enqueue, capacity, and
whether automatic admission is enabled. :func:`run_admission_pass` performs one
bounded write transaction and leaves omitted work pending. :class:`AdmissionLoop`
runs the pass in a worker thread at a fixed interval.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from aizk.graph.capacity import StageAtCapacityError, headroom
from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.graph.db import begin_immediate
from aizk.graph.enqueue import enqueue_output, pending_contextualization_outputs
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.extraction_workunit import enqueue_extraction, pending_extraction_sources
from aizk.pipeline.lifecycle import MAX_BULK_SELECTION

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine
    from sqlmodel import Session

    from aizk.graph.config import AdmissionConfig

logger = logging.getLogger(__name__)


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


def contextualization_adapter(config: "AdmissionConfig") -> AdmissionAdapter:
    """Declare the contextualization stage's admission over conversion outputs."""
    return AdmissionAdapter(
        stage=CONTEXTUALIZATION_STAGE,
        model=ContextualizationJob,
        pending_work=lambda session, limit: pending_contextualization_outputs(session, limit=limit),
        enqueue=lambda session, key, queue_max_depth, principal: enqueue_output(
            session, key, queue_max_depth=queue_max_depth, origin="admission", principal=principal
        ),
        queue_max_depth=config.contextualization_queue_max_depth,
        enabled=config.admission_contextualization_enabled,
    )


def extraction_adapter(config: "AdmissionConfig") -> AdmissionAdapter:
    """Declare the extraction stage's admission over chunked sources."""
    return AdmissionAdapter(
        stage=EXTRACTION_STAGE,
        model=ExtractionJob,
        pending_work=lambda session, limit: pending_extraction_sources(session, limit=limit),
        enqueue=lambda session, key, queue_max_depth, principal: enqueue_extraction(
            session, source_id=key, queue_max_depth=queue_max_depth, origin="admission", principal=principal
        ),
        queue_max_depth=config.extraction_queue_max_depth,
        enabled=config.admission_extraction_enabled,
    )


_ADAPTER_FACTORIES: "dict[str, Callable[[AdmissionConfig], AdmissionAdapter]]" = {
    CONTEXTUALIZATION_STAGE: contextualization_adapter,
    EXTRACTION_STAGE: extraction_adapter,
}


def admission_adapter_for(stage: str, config: "AdmissionConfig") -> AdmissionAdapter | None:
    """Return the adapter for ``stage``, or ``None`` when it has no admission path.

    The adapter reflects ``config``'s capacity and enabled flag.
    """
    factory = _ADAPTER_FACTORIES.get(stage)
    return factory(config) if factory is not None else None


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
