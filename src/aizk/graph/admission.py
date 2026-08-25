"""Admission: creating the work-units a stage's upstream state says should exist.

Admission is distinct from the runtime's *discovery*, which selects already-queued
units to claim. Admission asks a stage what work it is missing, and creates it.

A stage participates by declaring an :class:`AdmissionAdapter` — its pending-work
derivation, its enqueue primitive, its capacity, and whether automatic admission
is switched on. Declaring one is optional and queryable
(:func:`admission_adapter_for`): a stage that declares none is fully operable and
is simply never admitted into.

The pass itself (:func:`run_admission_pass`) is a bounded query plus an enqueue,
run inside one short write transaction. It has no memory: the pending set is a
function of current state, so work a pass leaves out — because the stage is at
capacity, or because the pass was interrupted — is still pending for the next
one. Because enqueue dedupes on ``idempotency_key``, a pass that overlaps an
intake submission or a backfill creates nothing twice.

:class:`AdmissionLoop` runs the pass on an interval inside a stage's existing
worker process, so admission needs no scheduler, no queue broker, and no new
process to supervise.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import TYPE_CHECKING, Any

from aizk.graph.capacity import StageAtCapacityError, headroom
from aizk.graph.datamodel import ContextualizationJob, ExtractionJob
from aizk.graph.db import begin_immediate
from aizk.graph.enqueue import enqueue_output, pending_contextualization_outputs
from aizk.graph.events import CONTEXTUALIZATION_STAGE
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.extraction_workunit import enqueue_extraction, pending_extraction_sources

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine
    from sqlmodel import Session

    from aizk.graph.config import AdmissionConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdmissionAdapter:
    """One stage's declaration of the work it can be admitted into.

    Attributes:
        stage: The stage this adapter admits work for.
        model: The stage's work-unit table, read to measure remaining capacity.
        pending_work: The stage's pending-work derivation, ``(session, limit) ->
            keys``. Each key identifies work that should have a work-unit and does
            not; the ``limit`` bounds the result after the derivation is applied.
        enqueue: The stage's enqueue primitive, ``(session, key, queue_max_depth)
            -> unit``. Admission goes through the stage's own enqueue rather than
            constructing rows, so an admitted unit is identical to one created by
            any other path.
        queue_max_depth: The stage's declared capacity; ``0`` declares no limit.
        enabled: Whether automatic admission is switched on for this stage.
            Off by default, so turning the flow on is a deliberate act.
    """

    stage: str
    model: Any
    pending_work: "Callable[[Session, int | None], list[Any]]"
    enqueue: "Callable[[Session, Any, int], Any]"
    queue_max_depth: int
    enabled: bool


def contextualization_adapter(config: "AdmissionConfig") -> AdmissionAdapter:
    """Declare the contextualization stage's admission over conversion outputs."""
    return AdmissionAdapter(
        stage=CONTEXTUALIZATION_STAGE,
        model=ContextualizationJob,
        pending_work=lambda session, limit: pending_contextualization_outputs(session, limit=limit),
        enqueue=lambda session, key, queue_max_depth: enqueue_output(session, key, queue_max_depth=queue_max_depth),
        queue_max_depth=config.contextualization_queue_max_depth,
        enabled=config.admission_contextualization_enabled,
    )


def extraction_adapter(config: "AdmissionConfig") -> AdmissionAdapter:
    """Declare the extraction stage's admission over chunked sources."""
    return AdmissionAdapter(
        stage=EXTRACTION_STAGE,
        model=ExtractionJob,
        pending_work=lambda session, limit: pending_extraction_sources(session, limit=limit),
        enqueue=lambda session, key, queue_max_depth: enqueue_extraction(
            session, source_id=key, queue_max_depth=queue_max_depth
        ),
        queue_max_depth=config.extraction_queue_max_depth,
        enabled=config.admission_extraction_enabled,
    )


_ADAPTER_FACTORIES: "dict[str, Callable[[AdmissionConfig], AdmissionAdapter]]" = {
    CONTEXTUALIZATION_STAGE: contextualization_adapter,
    EXTRACTION_STAGE: extraction_adapter,
}


def admission_adapter_for(stage: str, config: "AdmissionConfig") -> AdmissionAdapter | None:
    """Return the stage's admission adapter, or ``None`` when the stage declares none.

    This is the feature detection the contract rests on: a stage that declares no
    pending-work derivation reports none here, and no admission creates work for
    it. Its own enqueue and processing behavior is unaffected.

    Args:
        stage: The stage to query.
        config: The graph admission settings the adapter reads its capacity and
            enable flag from.
    """
    factory = _ADAPTER_FACTORIES.get(stage)
    return factory(config) if factory is not None else None


def run_admission_pass(engine: "Engine", adapter: AdmissionAdapter) -> int:
    """Admit a stage's pending work, up to its capacity, in one short write transaction.

    Creates work-units only for work in the stage's pending set, through the
    stage's own enqueue, so an admitted unit is identical to one any other path
    would create for the same work. Over unchanged state a repeat pass creates
    nothing, because the admitted work is no longer pending.

    Admission stops at capacity rather than overrunning it; what it does not admit
    stays pending for the next pass. A stage with automatic admission switched off
    admits nothing at all.

    Args:
        engine: The shared engine; the pass opens its own short transaction.
        adapter: The stage's declaration.

    Returns:
        How many work-units the pass created.
    """
    if not adapter.enabled:
        return 0

    admitted = 0
    with begin_immediate(engine) as session:
        room = headroom(session, adapter.model, limit=adapter.queue_max_depth)
        if room == 0:
            logger.info(
                "Admission for %s is at capacity; nothing admitted this pass",
                adapter.stage,
                extra={"stage": adapter.stage, "limit": adapter.queue_max_depth},
            )
            return 0
        pending = adapter.pending_work(session, room)
        for key in pending:
            try:
                adapter.enqueue(session, key, adapter.queue_max_depth)
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
    """Runs a stage's admission pass on an interval, in its worker process.

    Owns one background thread. :meth:`start` and :meth:`stop` bracket the
    worker's claim/execute loop; :meth:`stop` signals the thread and joins it, so
    a worker shutdown leaves no thread behind. A pass that raises is logged and
    the loop continues — admission is self-healing, so a transient failure costs
    one interval rather than the loop.
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
