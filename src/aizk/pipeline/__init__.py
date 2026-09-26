"""Pipeline-stage runtime primitives shared across processing stages.

This package holds the stage-agnostic primitives a processing stage builds on:

- the generic work-unit lifecycle and retry classification (:mod:`lifecycle`),
- the :class:`~aizk.pipeline.handler.StageHandler` protocol the runner
  drives over a stage's own store (:mod:`handler`),
- the stage kit: the static :class:`~aizk.pipeline.stage.StageDefinition`, the
  shared ``enqueue``, and :class:`~aizk.pipeline.stage.HandlerBase` over the
  :class:`~aizk.pipeline.repository.WorkUnitRepository` that owns claim, stale
  recovery, terminal transitions, operator retry, and operator cancel
  (:mod:`stage`, :mod:`repository`), with capacity over the actionable backlog
  (:mod:`capacity`),
- the :class:`~aizk.pipeline.runner.StageRunner` — the current embedded
  orchestration engine — with its per-instance
  :class:`~aizk.pipeline.shutdown.ShutdownController` (:mod:`runner`,
  :mod:`shutdown`),
- the stage-run / dataset-version primitive, with ``active_run``,
  ``require_active_run``, and ``record_applicability`` over it (:mod:`run`),
- the shared append-only transition-event log and ``record_transition`` helper
  (:mod:`events`), and
- the pipeline-identity grammar and the shared semantic ``derivation_key`` helper
  (:mod:`identity`), and
- lazy-invalidation staleness detection and the large-reprocessing confirmation
  gate (:mod:`invalidation`).

It is deliberately a set of primitives, not a framework: each stage owns its own
work-unit tables and identities and consumes these primitives through the
handler protocol. ``aizk.pipeline`` stays import-independent of
``aizk.conversion`` — consumers import the runtime, not vice versa.
"""

from __future__ import annotations

from aizk.pipeline.capacity import StageAtCapacityError, check_capacity, require_positive_limit, within_headroom
from aizk.pipeline.events import PipelineEvent, record_transition
from aizk.pipeline.handler import Isolation, StageHandler, StageResult, WorkUnitHandle
from aizk.pipeline.identity import DERIVATION_KEY_FORMAT, derivation_key
from aizk.pipeline.invalidation import (
    ReprocessingConfirmationError,
    generation_is_stale,
    require_reprocessing_confirmation,
    stale_active_generations,
)
from aizk.pipeline.lifecycle import (
    TERMINAL_STATUSES,
    RetryClass,
    TerminalOutcome,
    WorkUnitColumns,
    WorkUnitStatus,
    is_terminal,
)
from aizk.pipeline.repository import (
    CANCELLABLE_FROM,
    RETRYABLE_FROM,
    IneligibleTransitionError,
    WorkUnitRepository,
)
from aizk.pipeline.run import (
    PipelineRun,
    RunStatus,
    active_run,
    record_applicability,
    record_run,
    require_active_run,
    reuse_or_record_run,
)
from aizk.pipeline.runner import InMemoryMetrics, StageMetrics, StageRunner
from aizk.pipeline.stage import HandlerBase, StageDefinition, enqueue, require_stale_threshold

__all__ = [
    "CANCELLABLE_FROM",
    "DERIVATION_KEY_FORMAT",
    "RETRYABLE_FROM",
    "TERMINAL_STATUSES",
    "HandlerBase",
    "InMemoryMetrics",
    "IneligibleTransitionError",
    "Isolation",
    "PipelineEvent",
    "PipelineRun",
    "ReprocessingConfirmationError",
    "RetryClass",
    "RunStatus",
    "StageAtCapacityError",
    "StageDefinition",
    "StageRunner",
    "StageMetrics",
    "StageHandler",
    "StageResult",
    "TerminalOutcome",
    "WorkUnitColumns",
    "WorkUnitHandle",
    "WorkUnitRepository",
    "WorkUnitStatus",
    "active_run",
    "check_capacity",
    "derivation_key",
    "enqueue",
    "generation_is_stale",
    "is_terminal",
    "record_applicability",
    "record_run",
    "record_transition",
    "require_active_run",
    "require_positive_limit",
    "require_reprocessing_confirmation",
    "require_stale_threshold",
    "reuse_or_record_run",
    "stale_active_generations",
    "within_headroom",
]
