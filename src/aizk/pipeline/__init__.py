"""Pipeline-stage runtime primitives shared across processing stages.

This package holds the stage-agnostic primitives a processing stage builds on:

- the generic work-unit lifecycle and retry classification (:mod:`lifecycle`),
- the :class:`~aizk.pipeline.handler.StageHandler` protocol the runner
  drives over a stage's own store (:mod:`handler`),
- the :class:`~aizk.pipeline.runner.StageRunner` — the current embedded
  orchestration engine — with its per-instance
  :class:`~aizk.pipeline.shutdown.ShutdownController` (:mod:`runner`,
  :mod:`shutdown`),
- the stage-run / dataset-version primitive (:mod:`run`),
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

from aizk.pipeline.events import PipelineEvent, record_transition
from aizk.pipeline.handler import Isolation, StageHandler, StageResult, WorkUnitHandle
from aizk.pipeline.identity import derivation_key
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
    WorkUnitStatus,
    is_terminal,
)
from aizk.pipeline.run import PipelineRun, RunStatus, record_run, reuse_or_record_run
from aizk.pipeline.runner import InMemoryMetrics, StageMetrics, StageRunner

__all__ = [
    "TERMINAL_STATUSES",
    "InMemoryMetrics",
    "Isolation",
    "PipelineEvent",
    "PipelineRun",
    "ReprocessingConfirmationError",
    "RetryClass",
    "RunStatus",
    "StageRunner",
    "StageMetrics",
    "StageHandler",
    "StageResult",
    "TerminalOutcome",
    "WorkUnitHandle",
    "WorkUnitStatus",
    "derivation_key",
    "generation_is_stale",
    "is_terminal",
    "record_run",
    "record_transition",
    "require_reprocessing_confirmation",
    "reuse_or_record_run",
    "stale_active_generations",
]
