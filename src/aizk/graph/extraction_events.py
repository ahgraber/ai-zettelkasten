"""Define lifecycle event kinds and typed payloads for extraction.

Payloads are validated here before the shared pipeline event writer records them.
Events carry the stage and source identities and, when available, the run identity.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from aizk.graph.mention_store import MENTION_EXTRACTION_STAGE
from aizk.pipeline.events import CreationOrigin

#: Stage identifier stamped on the work-unit's transition events. Extraction's
#: runtime work-unit stage coincides exactly with the underlying
#: ``mention_extraction`` :class:`~aizk.pipeline.run.PipelineRun` stage — unlike
#: contextualization's work-unit stage, which spans three distinct underlying
#: run stages (chunking, document_summary, chunk_contextualization) — since
#: extraction produces exactly one run kind per source.
EXTRACTION_STAGE = MENTION_EXTRACTION_STAGE


class ExtractionEventKind(str, Enum):
    """Event kinds recorded for an extraction work-unit lifecycle."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
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
    """Payload for a successful transition, including the mention count."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["succeeded"] = "succeeded"
    mention_count: int


class FailedPayload(BaseModel):
    """Payload for a failed transition and its retry classification."""

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
