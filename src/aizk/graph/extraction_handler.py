"""Adapt extraction work-units to :class:`~aizk.pipeline.handler.StageHandler`.

The handler supplies the stage's dependencies, its unit of work, its result
mapping, and its success event; the lifecycle (claim, stale recovery, terminal
transition, cancel) is :class:`~aizk.pipeline.stage.HandlerBase` over the
work-unit repository. Domain extraction commits its own short transaction. An
in-process cancellation request is honored at execution entry, before
extraction, and before persist.
"""

from aizk.extraction.handler import (
    DEFAULT_CONCURRENCY,
    DEFAULT_RETRY_BASE_DELAY_SECONDS,
    DEFAULT_RETRY_MAX_ATTEMPTS,
    DEFAULT_STALE_AFTER_MINUTES,
    DEFAULT_TIMEOUT_SECONDS,
    ExtractionStageHandler,
)

__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_RETRY_BASE_DELAY_SECONDS",
    "DEFAULT_RETRY_MAX_ATTEMPTS",
    "DEFAULT_STALE_AFTER_MINUTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "ExtractionStageHandler",
]
