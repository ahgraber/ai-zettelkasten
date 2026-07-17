"""Transition-event kinds and typed payloads for the extraction stage.

The extraction stage's work-unit lifecycle transitions are recorded on the
shared ``pipeline_events`` log via :func:`aizk.pipeline.events.record_transition`.
That helper is stage-agnostic and validates only that the payload's ``kind``
matches the transition ``kind``; the per-kind field contract is owned here by
one pydantic model per kind (``extra="forbid"`` so an unknown field is
rejected on write).

The stage name stamped on every event is :data:`EXTRACTION_STAGE`; each event
also carries the ``source_id`` source identity and, where a run exists, the
``run_id`` of the work-unit's extraction run, so a source's progress is
resolvable across stages by a single ``source_id`` query.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from aizk.graph.mention_store import MENTION_EXTRACTION_STAGE

#: Stage identifier stamped on the work-unit's transition events. Extraction's
#: runtime work-unit stage coincides exactly with the underlying
#: ``mention_extraction`` :class:`~aizk.pipeline.run.PipelineRun` stage — unlike
#: contextualization's work-unit stage, which spans three distinct underlying
#: run stages (chunking, document_summary, chunk_contextualization) — since
#: extraction produces exactly one run kind per source.
EXTRACTION_STAGE = MENTION_EXTRACTION_STAGE


class ExtractionEventKind(str, Enum):
    """Event kinds recorded for an extraction work-unit's lifecycle.

    No ``skipped_superseded`` kind exists here: unlike contextualization's
    unit-of-work (which preflights a newer conversion output and short-circuits
    as a no-op), ``extract_document`` has no analogous freshness-supersession
    check — it always either persists mentions under the source's active
    extraction run or raises.
    """

    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
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

    Records the mention count the run produced so the audit row summarizes the
    unit's output without joining the mention table.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["succeeded"] = "succeeded"
    mention_count: int


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
