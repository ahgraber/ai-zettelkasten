"""Adapt contextualization work-units to :class:`~aizk.pipeline.handler.StageHandler`.

The handler supplies the stage's dependencies, its unit of work, its result
mapping, and its success event; the lifecycle (claim, stale recovery, terminal
transition, cancel) is :class:`~aizk.pipeline.stage.HandlerBase` over the
work-unit repository. The document domain operation commits its own short
transaction outside the runner transaction.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlmodel import Session

from aizk.contextualization.events import GraphEventKind, SkippedSupersededPayload, SucceededPayload
from aizk.contextualization.processing.document import ProcessResult, SkippedSuperseded, process_document
from aizk.contextualization.stage import CONTEXTUALIZATION_DEFINITION
from aizk.graph.datamodel import ContextualizationJob
from aizk.pipeline.handler import StageResult
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.pipeline.stage import HandlerBase
from aizk.utilities.clock import utcnow

if TYPE_CHECKING:
    from collections.abc import Callable
    import datetime

    from pydantic import BaseModel
    from sqlalchemy import Engine

    from aizk.ai.providers.llm import LLMClient, ModelConfig
    from aizk.contextualization.processing.document import MarkdownSource, OutputFreshness

logger = logging.getLogger(__name__)

#: Default wall-clock timeout for one document's contextualization unit-of-work.
DEFAULT_TIMEOUT_SECONDS = 600.0
#: Default execution concurrency. One preserves the single serialized SQLite writer.
DEFAULT_CONCURRENCY = 1
#: Default minutes a unit may sit in ``RUNNING`` before stale recovery reclaims it.
DEFAULT_STALE_AFTER_MINUTES = 30.0
#: Default retry backoff base (seconds); the wait is ``base * 2**attempts``.
DEFAULT_RETRY_BASE_DELAY_SECONDS = 2.0
#: Default maximum attempts before a retryable failure becomes permanent.
DEFAULT_RETRY_MAX_ATTEMPTS = 3


def _error_code(exc: BaseException) -> str:
    """Classify an exception into a short, stable error code for the audit row."""
    if isinstance(exc, ValueError):
        return "unprocessable_input"
    return type(exc).__name__


def _error_message(exc: BaseException) -> str:
    """Return a stable, scrubbed category message for the audit row and operator API.

    Raw exception text (provider, S3, or DB errors) may embed object keys,
    endpoints, or SQL fragments; it is persisted into ``error_message`` and
    returned by the operator API, so it must not carry that detail across the
    boundary. Only this category text is persisted/returned — the precise stable
    classifier stays in :func:`_error_code`, and the full exception (with
    traceback) is logged internally in :meth:`ContextualizationStageHandler.execute`
    for diagnosis.
    """
    if isinstance(exc, ValueError):
        return "input could not be processed"
    return "a transient processing error occurred"


class ContextualizationStageHandler(HandlerBase[ContextualizationJob]):
    """Drive contextualization work-units through the generic pipeline runner.

    The work-unit handle is the integer ``ContextualizationJob.id``. The model
    and Markdown source are injected dependencies, so a deterministic substitute
    drives the stage in tests with no change to this logic.
    """

    default_error = ("contextualization_error", "unknown error")

    def __init__(
        self,
        engine: "Engine",
        llm_client: "LLMClient",
        markdown_source: "MarkdownSource",
        freshness: "OutputFreshness",
        *,
        model_config: "ModelConfig",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES,
        retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS,
        clock: "Callable[[], datetime.datetime]" = utcnow,
    ) -> None:
        """Store the injected dependencies and hand the runner-facing configuration to the base.

        Args:
            engine: The shared SQLite engine; the unit-of-work opens its own short
                ``BEGIN IMMEDIATE`` for the domain writes (after the lock-free
                fetch/generate phase).
            llm_client: The single model access point for the summary and variant
                passes.
            markdown_source: The seam that fetches a document's Markdown by
                ``conversion_output_id``.
            freshness: The seam deciding whether a conversion output is still the
                source's latest (the monotonic-currentness gate).
            model_config: The resolved model identity passed to
                :func:`~aizk.contextualization.processing.document.process_document` for every unit.
            timeout_seconds: Wall-clock timeout per unit (the runner enforces it).
            concurrency: Maximum units the runner may execute simultaneously.
            stale_after_minutes: Minutes in ``RUNNING`` before recovery reclaims a
                unit.
            retry_base_delay_seconds: Retry backoff base; wait is
                ``base * 2**attempts``.
            retry_max_attempts: Attempt cap after which a retryable failure
                becomes permanent.
            clock: Source of every timestamp the lifecycle writes.
        """
        super().__init__(
            engine,
            CONTEXTUALIZATION_DEFINITION,
            timeout_seconds=timeout_seconds,
            concurrency=concurrency,
            stale_after_minutes=stale_after_minutes,
            retry_base_delay_seconds=retry_base_delay_seconds,
            retry_max_attempts=retry_max_attempts,
            clock=clock,
        )
        self._llm_client = llm_client
        self._markdown_source = markdown_source
        self._freshness = freshness
        self._model_config = model_config
        # Per-handle bridges across the execute/finalize boundary (finalize is
        # handed only (session, handle, outcome)): the produced chunking run id +
        # variant count for the SUCCEEDED event and the superseding-output id for
        # a SKIPPED_SUPERSEDED event, beside the base's scrubbed error pair.
        # Guarded by the base's lock (N worker threads race). finalize reads them
        # without clearing; cleanup() releases them exactly once, only after the
        # durable terminal transition commits — so a runner finalize-retry (on a
        # finalize-time DB error, slot retained) still sees them.
        self._success: dict[int, tuple[int, int]] = {}
        self._skipped: dict[int, int] = {}

    def validate_dependencies(self) -> None:
        """No external probes: the model and Markdown source are injected dependencies."""
        return None

    def map_result(self, result_or_exc: StageResult | BaseException) -> TerminalOutcome:
        """Map an execution result or exception to a terminal outcome.

        A successful result maps to ``SUCCEEDED``. A :class:`ValueError` — the
        type raised for a guardrail rejection (over-budget summary/revision) or
        unprocessable input (markdown-hash drift, provenance mismatch) — maps to
        a ``permanent`` ``FAILED``; any other exception (a transient model or I/O
        error) maps to a ``retryable`` ``FAILED``. ``TIMED_OUT`` and ``CANCELLED``
        are resolved by the runner from its slot state for in-process units and
        do not pass through here.
        """
        if isinstance(result_or_exc, BaseException):
            if isinstance(result_or_exc, ValueError):
                return TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.PERMANENT)
            return TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE)
        return TerminalOutcome(WorkUnitStatus.SUCCEEDED)

    def execute(self, handle: int) -> StageResult:
        """Run the unit-of-work for ``handle``; bridge result/error details.

        Resolves the work-unit's source identity, honors an entry-time
        cancellation, then runs :func:`~aizk.contextualization.processing.document.process_document`
        (which keeps the S3 fetch + LLM passes outside its own short
        ``BEGIN IMMEDIATE`` write transaction). Stashes the chunking run id +
        variant count for the ``SUCCEEDED`` event, or the superseding-output id for
        a ``SKIPPED_SUPERSEDED`` event; on any :class:`Exception` stashes the
        scrubbed error code + message for the ``FAILED`` event and re-raises so
        ``map_result`` classifies it.

        Raises:
            ValueError: If the work-unit row is missing (unprocessable input).
        """
        with Session(self._engine) as session:
            job = session.get(ContextualizationJob, handle)
            if job is None:
                raise ValueError(f"contextualization work-unit {handle} is missing")
            # An operator may have cancelled the unit before execution started; do
            # no work (finalize honors the terminal CANCELLED).
            if job.status is WorkUnitStatus.CANCELLED:
                logger.info("contextualization work-unit %s already cancelled; skipping execution", handle)
                return None
            source_id = job.source_id
            conversion_output_id = job.conversion_output_id

        # A runner cancel/timeout already requested before work started: skip.
        if self._is_cancelled(handle):
            logger.info("contextualization work-unit %s cancelled before start; skipping execution", handle)
            return None

        try:
            result = process_document(
                self._engine,
                self._llm_client,
                source_id=source_id,
                conversion_output_id=conversion_output_id,
                markdown_source=self._markdown_source,
                freshness=self._freshness,
                model_config=self._model_config,
                is_cancelled=lambda: self._is_cancelled(handle),
            )
        except Exception as exc:
            # Full detail (including the raw message) stays in the internal log; only
            # the stable code + scrubbed category message cross into the audit row/API.
            logger.exception("contextualization work-unit %s failed during execution", handle)
            with self._lock:
                self._errors[handle] = (_error_code(exc), _error_message(exc))
            raise

        with self._lock:
            if isinstance(result, ProcessResult):
                self._success[handle] = (result.chunking_run_id, result.variant_count)
            elif isinstance(result, SkippedSuperseded):
                self._skipped[handle] = result.conversion_output_id
            # A Cancelled result writes nothing; the runner resolves the terminal
            # CANCELLED / TIMED_OUT status from its slot state and finalize records it.
        return result

    def succeeded_event(self, handle: int) -> "tuple[Any, BaseModel, int | None]":
        """Return the success event for ``handle`` from the execute bridges.

        A unit a newer conversion output superseded did nothing and succeeds
        as a no-op, recorded distinctly as ``skipped_superseded``; otherwise
        the ``succeeded`` event carries the variant count and the chunking
        ``run_id``.
        """
        with self._lock:
            success = self._success.get(handle)
            skipped_output_id = self._skipped.get(handle)
        if skipped_output_id is not None:
            return GraphEventKind.SKIPPED_SUPERSEDED, SkippedSupersededPayload(superseding_output_id=None), None
        run_id, variant_count = success if success is not None else (None, 0)
        return GraphEventKind.SUCCEEDED, SucceededPayload(variant_count=variant_count), run_id

    def cleanup(self, handle: int) -> None:
        """Release the unit's per-handle bridge entries; called on every outcome."""
        super().cleanup(handle)
        with self._lock:
            self._success.pop(handle, None)
            self._skipped.pop(handle, None)
