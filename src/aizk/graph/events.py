"""Transition-event kinds and typed payloads for the contextualization stage.

The graph stage's work-unit lifecycle transitions are recorded on the shared
``pipeline_events`` log via :func:`aizk.pipeline.events.record_transition`. That
helper is stage-agnostic and validates only that the payload's ``kind`` matches
the transition ``kind``; the per-kind field contract is owned here by one
pydantic model per kind (``extra="forbid"`` so an unknown field is rejected on
write).

The stage name stamped on every event is :data:`CONTEXTUALIZATION_STAGE`; each
event also carries the ``aizk_uuid`` source identity and, where a run exists, the
``run_id`` of the work-unit's chunking run, so a source's progress is resolvable
across stages by a single ``aizk_uuid`` query.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

#: Stage identifier stamped on the work-unit's transition events.
CONTEXTUALIZATION_STAGE = "contextualization"


class GraphEventKind(str, Enum):
    """Event kinds recorded for a contextualization work-unit's lifecycle."""

    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    SKIPPED_SUPERSEDED = "skipped_superseded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    RECOVERED_STALE = "recovered_stale"
    REQUEUED = "requeued"


class ClaimedPayload(BaseModel):
    """Payload for the ``QUEUED/FAILED → RUNNING`` claim transition."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["claimed"] = "claimed"
    claimed_at: datetime.datetime
    worker_pid: int | None = None


class SucceededPayload(BaseModel):
    """Payload for the ``RUNNING → SUCCEEDED`` terminal transition.

    Records the variant count the run produced (one per persisted chunk) so the
    audit row summarizes the unit's output without joining the run tables.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["succeeded"] = "succeeded"
    variant_count: int


class SkippedSupersededPayload(BaseModel):
    """Payload for a unit that did nothing because a newer source output already won.

    Recorded when the work-unit's conversion output is no longer the latest for
    its source: the older unit reaches ``SUCCEEDED`` as a no-op (it correctly
    wrote nothing) rather than superseding the newer generation's runs.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["skipped_superseded"] = "skipped_superseded"
    superseding_output_id: int | None = None


class FailedPayload(BaseModel):
    """Payload for a ``RUNNING → FAILED`` terminal transition.

    ``retryable`` records whether the unit remains eligible for another attempt
    (within the attempt cap) or has reached a permanent failure.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["failed"] = "failed"
    error_code: str
    error_message: str
    retryable: bool


class CancelledPayload(BaseModel):
    """Payload for a ``RUNNING → CANCELLED`` terminal transition."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["cancelled"] = "cancelled"
    cancellation_reason: str


class TimedOutPayload(BaseModel):
    """Payload for a ``RUNNING → TIMED_OUT`` terminal transition."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["timed_out"] = "timed_out"
    timeout_seconds: float


class RecoveredStalePayload(BaseModel):
    """Payload for a stale ``RUNNING`` unit reset to eligible by stale recovery."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["recovered_stale"] = "recovered_stale"
    stale_after_minutes: float
    last_started_at: datetime.datetime | None = None


class RequeuedPayload(BaseModel):
    """Payload for an operator-driven retry that re-queues a terminal unit."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["requeued"] = "requeued"
    requeue_reason: str
