"""Define lifecycle event kinds and typed payloads for contextualization.

Payloads are validated here before the shared pipeline event writer records them.
Events carry the stage and source identities and, when available, the run identity.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from aizk.pipeline.events import CreationOrigin

#: Stage identifier stamped on the work-unit's transition events.
CONTEXTUALIZATION_STAGE = "contextualization"


class GraphEventKind(str, Enum):
    """Event kinds recorded for a contextualization work-unit's lifecycle."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    SKIPPED_SUPERSEDED = "skipped_superseded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    RECOVERED_STALE = "recovered_stale"
    REQUEUED = "requeued"


class QueuedPayload(BaseModel):
    """Payload for the creation event: the path that created the unit and the acting principal."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["queued"] = "queued"
    origin: CreationOrigin
    principal: str


class ClaimedPayload(BaseModel):
    """Payload for the ``QUEUED/FAILED → RUNNING`` claim transition."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["claimed"] = "claimed"
    claimed_at: datetime.datetime
    worker_pid: int | None = None


class SucceededPayload(BaseModel):
    """Payload for a successful contextualization transition.

    ``variant_count`` records the number of persisted variants.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["succeeded"] = "succeeded"
    variant_count: int


class SkippedSupersededPayload(BaseModel):
    """Payload for a unit skipped because a newer source output superseded it."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["skipped_superseded"] = "skipped_superseded"
    superseding_output_id: int | None = None


class FailedPayload(BaseModel):
    """Payload for a failed transition, including retry eligibility."""

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
    """Payload for an operator-driven retry that re-queues a terminal unit: the reason and the acting principal."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["requeued"] = "requeued"
    requeue_reason: str
    principal: str
