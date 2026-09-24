"""Adapt extraction work-units to :class:`~aizk.pipeline.handler.StageHandler`.

The handler supplies the stage's dependencies, its unit of work, its result
mapping, and its success event; the lifecycle (claim, stale recovery, terminal
transition, cancel) is :class:`~aizk.pipeline.stage.HandlerBase` over the
work-unit repository. Domain extraction commits its own short transaction. An
in-process cancellation request is honored at execution entry, before
extraction, and before persist.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlmodel import Session

from aizk.extraction.events import ExtractionEventKind, SucceededPayload
from aizk.extraction.processing.document import DocumentExtractionResult, extract_document
from aizk.extraction.stage import EXTRACTION_DEFINITION
from aizk.graph.datamodel import ExtractionJob
from aizk.pipeline.handler import StageResult
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus
from aizk.pipeline.stage import HandlerBase
from aizk.utilities.clock import utcnow

if TYPE_CHECKING:
    from collections.abc import Callable
    import datetime

    from pydantic import BaseModel
    from sqlalchemy import Engine

    from aizk.extraction.core.extractors import EntityExtractor
    from aizk.graph.mentions import InputPolicy

logger = logging.getLogger(__name__)

#: Default wall-clock timeout for one source's extraction unit-of-work.
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
    """Classify an exception into a stable error code."""
    if isinstance(exc, ValueError):
        return "unprocessable_input"
    return type(exc).__name__


def _error_message(exc: BaseException) -> str:
    """Return a scrubbed category message for persistence and the operator API.

    The function excludes raw exception text from those boundaries.
    """
    if isinstance(exc, ValueError):
        return "input could not be processed"
    return "a transient processing error occurred"


class ExtractionStageHandler(HandlerBase[ExtractionJob]):
    """Drive extraction work-units through the generic pipeline runner.

    The handler receives the extractor and input policy as dependencies.
    """

    default_error = ("extraction_error", "unknown error")

    def __init__(
        self,
        engine: "Engine",
        extractor: "EntityExtractor",
        *,
        input_policy: "InputPolicy",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        stale_after_minutes: float = DEFAULT_STALE_AFTER_MINUTES,
        retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
        retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS,
        clock: "Callable[[], datetime.datetime]" = utcnow,
    ) -> None:
        """Configure the extraction stage handler.

        Args:
            engine: Shared engine used by extraction operations.
            extractor: Injected NER extractor.
            input_policy: Input mode for extraction.
            timeout_seconds: Per-unit wall-clock timeout.
            concurrency: Maximum concurrent units.
            stale_after_minutes: Age for stale-unit recovery.
            retry_base_delay_seconds: Base retry delay.
            retry_max_attempts: Maximum attempts before permanent failure.
            clock: Source of every timestamp the lifecycle writes.
        """
        super().__init__(
            engine,
            EXTRACTION_DEFINITION,
            timeout_seconds=timeout_seconds,
            concurrency=concurrency,
            stale_after_minutes=stale_after_minutes,
            retry_base_delay_seconds=retry_base_delay_seconds,
            retry_max_attempts=retry_max_attempts,
            clock=clock,
        )
        self._extractor = extractor
        self._input_policy = input_policy
        # Per-handle bridge across the execute/finalize boundary: the produced
        # extraction run id + mention count for the SUCCEEDED event, beside the
        # base's scrubbed error pair. Guarded by the base's lock (N worker
        # threads race). finalize reads it without clearing; cleanup() releases
        # it exactly once, only after the durable terminal transition commits —
        # so a runner finalize-retry (on a finalize-time DB error, slot
        # retained) still sees it.
        self._success: dict[int, tuple[int, int]] = {}

    def validate_dependencies(self) -> None:
        """Validate dependencies after the composition root constructs the extractor."""
        return None

    def map_result(self, result_or_exc: StageResult | BaseException) -> TerminalOutcome:
        """Map execution results and exceptions to terminal outcomes.

        Success maps to ``SUCCEEDED``. ``ValueError`` maps to permanent ``FAILED``;
        other exceptions map to retryable ``FAILED``. The runner handles cancellation
        and timeouts.
        """
        if isinstance(result_or_exc, BaseException):
            if isinstance(result_or_exc, ValueError):
                # Every ValueError the write path raises today is a deterministic
                # provenance/input violation (unchunked source, invalid policy,
                # dangling locator, bad span), so a retry cannot succeed. If a
                # future compaction/retention change makes variant locators
                # transiently dangle, this classification needs revisiting.
                return TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.PERMANENT)
            return TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE)
        return TerminalOutcome(WorkUnitStatus.SUCCEEDED)

    def execute(self, handle: int) -> StageResult:
        """Execute one extraction work-unit.

        Skip work already cancelled. Store success or scrubbed failure details for
        finalization, and re-raise execution errors.

        Raises:
            ValueError: If the work-unit or its extraction input is invalid.
        """
        with Session(self._engine) as session:
            job = session.get(ExtractionJob, handle)
            if job is None:
                raise ValueError(f"extraction work-unit {handle} is missing")
            # An operator may have cancelled the unit before execution started; do
            # no work (finalize honors the terminal CANCELLED).
            if job.status is WorkUnitStatus.CANCELLED:
                logger.info("extraction work-unit %s already cancelled; skipping execution", handle)
                return None
            source_id = job.source_id

        # A runner cancel/timeout already requested before work started: skip.
        if self._is_cancelled(handle):
            logger.info("extraction work-unit %s cancelled before start; skipping execution", handle)
            return None

        try:
            result = extract_document(
                self._engine,
                source_id=source_id,
                extractor=self._extractor,
                input_policy=self._input_policy,
                is_cancelled=lambda: self._is_cancelled(handle),
            )
        except Exception as exc:
            # Full detail (including the raw message) stays in the internal log; only
            # the stable code + scrubbed category message cross into the audit row/API.
            logger.exception("extraction work-unit %s failed during execution", handle)
            with self._lock:
                self._errors[handle] = (_error_code(exc), _error_message(exc))
            raise

        with self._lock:
            if isinstance(result, DocumentExtractionResult):
                self._success[handle] = (result.run_id, result.mention_count)
        return result

    def succeeded_event(self, handle: int) -> "tuple[Any, BaseModel, int | None]":
        """Return the ``succeeded`` event for ``handle``: the mention count and the extraction ``run_id``."""
        with self._lock:
            success = self._success.get(handle)
        run_id, mention_count = success if success is not None else (None, 0)
        return ExtractionEventKind.SUCCEEDED, SucceededPayload(mention_count=mention_count), run_id

    def cleanup(self, handle: int) -> None:
        """Release the unit's per-handle bridge entries; called on every outcome."""
        super().cleanup(handle)
        with self._lock:
            self._success.pop(handle, None)
