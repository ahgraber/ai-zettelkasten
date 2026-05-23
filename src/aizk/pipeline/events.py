"""Shared append-only transition-event log for all pipeline stages.

A single ``pipeline_events`` table holds the durable transition events for every
stage. Each row carries the ``stage``, the stage's own ``work_unit_ref``, an
optional ``run_id``, and the ``aizk_uuid`` source identity, so a source's
progress is resolvable across stages by a single query on ``aizk_uuid`` with no
per-stage joins.

:func:`record_transition` co-commits the event with the work-unit's status
change: it mutates the work-unit's stage-specific status attribute and stages
the matching event row in the same session, so a committed status change never
exists without its event and vice versa. Calling conventions mirror conversion's
original helper:

1. **Callers own commit boundaries.** The helper calls ``session.add(...)`` only;
   it never commits. Callers run inside a ``BEGIN IMMEDIATE`` transaction (the
   harness's claim/finalize block) that owns commit semantics.
2. **Stage-owned typed payloads.** Each stage defines its own per-kind pydantic
   payload models (the typed contract for a kind). The helper validates that the
   supplied payload declares a ``kind`` matching the transition ``kind`` and
   serializes it; the per-kind field contract is enforced by the stage's model.
3. **Generic text columns.** ``stage``, ``work_unit_ref``, ``kind``,
   ``from_status``, and ``to_status`` are stored as text so the table stays
   stage-agnostic; stage-specific status/kind enums are rendered to their
   string values on write.
"""

from __future__ import annotations

import datetime
from enum import Enum
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Index, Text, func
from sqlmodel import Field, SQLModel

if TYPE_CHECKING:
    from sqlmodel import Session

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc)


class _UnsetType:
    """Sentinel distinguishing a defaulted ``from_status`` from an explicit ``None``."""

    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET: _UnsetType = _UnsetType()


def _as_text(value: Any) -> str | None:
    """Render a status/kind value to its stored text form.

    Enum members render to their ``.value``; ``None`` passes through; anything
    else is coerced with ``str``.
    """
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


class PipelineEvent(SQLModel, table=True):
    """Durable record of a single work-unit lifecycle event, shared across stages.

    Rows are append-only. ``work_unit_ref`` and ``run_id`` are logical
    references (text / id, no database foreign keys) so the table stays
    decoupled from any single stage's schema and survives operator deletion of
    a stage's work-unit row; ``aizk_uuid`` is the denormalized source identity
    that keeps the audit trail queryable after such deletions.

    Column nullability conventions match conversion's original event log:

    - ``from_status`` is NULL only on origin events (no prior committed state).
    - ``to_status`` is NULL only on non-transition events (writes to other
      entities that do not assert a target work-unit status).
    """

    __tablename__ = "pipeline_events"
    __table_args__ = (
        Index(
            "ix_pipeline_events_aizk_uuid_occurred_at",
            "aizk_uuid",
            "occurred_at",
        ),
        Index(
            "ix_pipeline_events_stage_work_unit_ref_occurred_at",
            "stage",
            "work_unit_ref",
            "occurred_at",
        ),
        Index(
            "ix_pipeline_events_kind_occurred_at",
            "kind",
            "occurred_at",
        ),
    )

    event_id: int | None = Field(default=None, primary_key=True, nullable=False)
    stage: str = Field(sa_column=Column(Text, nullable=False))
    work_unit_ref: str = Field(sa_column=Column(Text, nullable=False))
    run_id: int | None = Field(default=None, nullable=True)
    aizk_uuid: UUID = Field(nullable=False)
    from_status: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    to_status: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    kind: str = Field(sa_column=Column(Text, nullable=False))
    attempt: int | None = Field(default=None, nullable=True)
    occurred_at: datetime.datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            DateTime(),
            nullable=False,
            server_default=func.current_timestamp(),
        ),
    )
    payload_json: str = Field(sa_column=Column(Text, nullable=False))


def record_transition(
    session: "Session",
    work_unit: Any,
    *,
    stage: str,
    work_unit_ref: str,
    aizk_uuid: UUID,
    to_status: Any,
    kind: Any,
    payload: BaseModel,
    attempt: int | None = None,
    run_id: int | None = None,
    from_status: Any | _UnsetType = _UNSET,
    status_attr: str = "status",
) -> PipelineEvent:
    """Record a work-unit status transition and its event row in one transaction.

    Reads the work-unit's current status (via ``status_attr``) as the event's
    ``from_status`` unless one is given explicitly, mutates the work-unit to
    ``to_status``, and stages one :class:`PipelineEvent` carrying the typed
    ``payload``. Both the status change and the event are added to ``session``;
    the caller's surrounding transaction commits them together. Does **not**
    commit.

    For origin events (the first event for a brand-new work-unit), pass
    ``from_status=None`` explicitly.

    Args:
        session: Active session; the caller owns commit/rollback.
        work_unit: The stage's work-unit ORM object whose status is mutated.
        stage: Stage identifier stored on the event.
        work_unit_ref: The stage's own identity for the work-unit (text).
        aizk_uuid: Source identity, denormalized for cross-stage queries.
        to_status: New status to apply to the work-unit (enum or str); rendered
            to text on the event.
        kind: Event kind for the audit row; must match ``payload``'s ``kind``.
        payload: Stage-defined typed payload model for the kind.
        attempt: Optional attempt number the event belongs to.
        run_id: Optional run this work-unit's outputs belong to.
        from_status: Optional override for the event's prior-status column;
            when omitted, read from the work-unit before mutation. Pass ``None``
            explicitly for origin events.
        status_attr: Name of the work-unit's status attribute to mutate.

    Returns:
        The constructed (``session.add``-staged) :class:`PipelineEvent`.

    Note:
        This helper validates kind consistency only. Stage-owned typed payload
        validation (asserting the payload is the correct model for a kind) is
        the caller's responsibility; Pydantic enforces it at construction time.

    Raises:
        ValueError: If ``payload`` declares no ``kind`` or its ``kind`` does not
            match the transition ``kind``.
    """
    payload_kind = getattr(payload, "kind", None)
    if payload_kind is None:
        raise ValueError("payload must declare a 'kind' field")
    if _as_text(payload_kind) != _as_text(kind):
        raise ValueError(f"payload kind {payload_kind!r} does not match transition kind {kind!r}")
    payload_json = payload.model_dump_json()

    if isinstance(from_status, _UnsetType):
        prior_status: Any = getattr(work_unit, status_attr)
    else:
        prior_status = from_status
    setattr(work_unit, status_attr, to_status)

    event = PipelineEvent(
        stage=stage,
        work_unit_ref=work_unit_ref,
        run_id=run_id,
        aizk_uuid=aizk_uuid,
        from_status=_as_text(prior_status),
        to_status=_as_text(to_status),
        kind=str(_as_text(kind)),
        attempt=attempt,
        payload_json=payload_json,
    )

    session.add(work_unit)
    session.add(event)
    return event
