"""Pipeline-stage runtime primitives shared across processing stages.

This package holds the stage-agnostic primitives a processing stage builds on:

- the generic work-unit lifecycle and retry classification (:mod:`lifecycle`),
- the :class:`~aizk.pipeline.repository.StageRepository` protocol the harness
  drives over a stage's own store (:mod:`repository`),
- the stage-run / dataset-version primitive (:mod:`run`), and
- the shared append-only transition-event log and ``record_transition`` helper
  (:mod:`events`).

It is deliberately a set of primitives, not a framework: each stage owns its own
work-unit tables and identities and consumes these primitives through the
repository protocol. ``aizk.pipeline`` stays import-independent of
``aizk.conversion`` — consumers import the runtime, not vice versa.
"""

from __future__ import annotations

from aizk.pipeline.events import PipelineEvent, record_transition
from aizk.pipeline.lifecycle import (
    TERMINAL_STATUSES,
    RetryClass,
    TerminalOutcome,
    WorkUnitStatus,
    is_terminal,
)
from aizk.pipeline.repository import Isolation, StageRepository, StageResult, WorkUnitHandle
from aizk.pipeline.run import PipelineRun, RunStatus, record_run

__all__ = [
    "TERMINAL_STATUSES",
    "Isolation",
    "PipelineEvent",
    "PipelineRun",
    "RetryClass",
    "RunStatus",
    "StageRepository",
    "StageResult",
    "TerminalOutcome",
    "WorkUnitHandle",
    "WorkUnitStatus",
    "is_terminal",
    "record_run",
    "record_transition",
]
