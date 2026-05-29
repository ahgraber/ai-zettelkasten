"""Conversion-stage transition events on the shared ``pipeline_events`` log.

This module defines the conversion stage's typed event vocabulary
(:class:`ConversionEventKind` plus one validated pydantic payload model per
kind) and the ``record_transition`` / ``record_phase_event`` /
``record_source_event`` helpers that every status-mutating site in the worker
and API funnels through.

The durable rows now live in the shared :class:`aizk.pipeline.events.PipelineEvent`
table (``pipeline_events``), not in a conversion-private table: these helpers
validate the conversion-specific payload, then delegate to
:func:`aizk.pipeline.events.record_transition`, recording the row with
``stage="conversion"``, ``work_unit_ref=str(job.id)``, the denormalized
``aizk_uuid``, the kind/from-status/to-status rendered to text, the attempt, and
the validated payload serialized to the generic ``payload_json`` column. The
conversion event history is therefore queryable both per-job (``stage`` +
``work_unit_ref``) and cross-stage (``aizk_uuid``); use :func:`events_for_job`
to read a single job's conversion events back in occurrence order.

Calling conventions:

1. **Callers own commit boundaries.** ``record_transition`` and its siblings
   call ``session.add(...)`` only; they do not call ``session.commit()``.
   This is required because callers run inside heterogeneous transaction
   shapes (API submit's ``BEGIN IMMEDIATE`` block, worker claim's
   ``BEGIN IMMEDIATE`` block, orchestrator's implicit transaction) that
   already own commit semantics.
2. **``attempt`` is an explicit required parameter**, not inferred from
   ``job.attempts``. Some sites (claim, retry) increment ``job.attempts``
   before calling the helper; others (cancel, stale-recovery, source
   enrichment) do not. Forcing the caller to state the attempt makes the
   answer part of code review rather than a hidden invariant.
3. **Write-strict, read-lenient pydantic posture.** Each per-kind payload
   model uses ``extra="forbid"`` so a typo or stale field surfaces as
   ``ValidationError`` at insertion. Reads go through
   ``parse_payload_lenient``, which pre-filters unrecognized keys against
   the variant's declared field set before constructing the model — so an
   older row whose payload carries an additive field that current code does
   not yet recognize still deserializes cleanly. (The lenience is
   implemented as field-name filtering rather than as a separate
   ``extra="ignore"`` model_config, but the behavioral guarantee is the
   same.)
4. **Versioning via new ``kind`` variants.** Incompatible payload changes
   (renamed field, removed field, changed type) SHALL be expressed by
   introducing a new ``kind`` to ``ConversionEventKind`` (e.g.,
   ``failed`` → ``failed_v2``) rather than mutating an existing kind's
   contract. Additive changes (new optional fields on an existing kind)
   are tolerated by ``extra="ignore"`` on read.

See ``.specs/changes/pipeline-stage-runtime/design.md § PipelineEventsGenericSchema``
for the relocation rationale and ``.specs/changes/archive/2026-05-18-conversion-job-event-log/design.md``
for the original helper-calling-convention and typed-payload rationale.
"""

from __future__ import annotations

import datetime
from enum import Enum
import json
import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field as PydField, TypeAdapter, ValidationError
from sqlmodel import select

from aizk.conversion.datamodel.job import ConversionJobStatus
from aizk.pipeline.events import PipelineEvent

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.conversion.datamodel.job import ConversionJob

logger = logging.getLogger(__name__)

#: Stage identifier stamped on every conversion event row in ``pipeline_events``.
STAGE = "conversion"


class _UnsetType:
    """Sentinel type for distinguishing default vs explicit None on optional args."""

    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET: _UnsetType = _UnsetType()


class ConversionEventKind(str, Enum):
    """Closed enumeration of conversion-job event kinds.

    Each variant has a corresponding pydantic payload model in this module
    that defines the permitted fields for the kind. New kinds are added here
    when an incompatible payload change is needed for an existing kind
    (e.g., ``failed`` → ``failed_v2``) — never mutate an existing variant's
    contract in place.
    """

    QUEUED = "queued"
    CLAIMED = "claimed"
    PHASE = "phase"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    UPLOAD_PENDING = "upload_pending"
    RECOVERED_STALE = "recovered_stale"
    SOURCE_ENRICHED = "source_enriched"


# ---------------------------------------------------------------------------
# Typed payload variants (one per kind)
# ---------------------------------------------------------------------------


class QueuedPayload(BaseModel):
    """Payload for a transition into QUEUED (initial submission or retry requeue)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["queued"] = "queued"
    submitted_by: str | None = None
    requeue_reason: Literal["initial", "retry_endpoint"]


class ClaimedPayload(BaseModel):
    """Payload for a worker claiming a job and moving it to RUNNING."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["claimed"] = "claimed"
    claimed_at: datetime.datetime
    worker_pid: int | None = None


class PhasePayload(BaseModel):
    """Payload for a subprocess-reported progress phase within RUNNING."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["phase"] = "phase"
    phase: Literal["preparing_input", "converting", "uploading"]
    reported_at: datetime.datetime


class CancelledPayload(BaseModel):
    """Payload for a transition into CANCELLED."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["cancelled"] = "cancelled"
    cancelled_by: str | None = None
    cancellation_reason: str | None = None


class FailedPayload(BaseModel):
    """Payload for a transition into FAILED_RETRYABLE or FAILED_PERM.

    ``error_message`` and ``error_detail`` carry the *post-sanitization*
    values written to ``ConversionJob.error_message`` and
    ``ConversionJob.error_detail`` — for egress-policy errors these are the
    bare error code and ``None`` respectively (rejected destinations never
    land in durable storage).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["failed"] = "failed"
    error_code: str
    error_message: str
    error_detail: str | None = None
    retryable: bool
    last_phase: str | None = None


class SucceededPayload(BaseModel):
    """Payload for a transition into SUCCEEDED."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["succeeded"] = "succeeded"
    output_id: int
    content_hash: str


class UploadPendingPayload(BaseModel):
    """Payload for a transition into UPLOAD_PENDING.

    ``content_hash`` is optional because the transition is recorded even
    when ``SubprocessMetadata`` failed to load — the subsequent upload
    will then fail at ``_prepare_upload`` and surface its own ``failed``
    event, but the audit log still captures the attempted phase entry.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["upload_pending"] = "upload_pending"
    content_hash: str | None = None


class RecoveredStalePayload(BaseModel):
    """Payload for a stale-RUNNING-job recovery sweep transition."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["recovered_stale"] = "recovered_stale"
    stale_after_minutes: int
    last_started_at: datetime.datetime | None = None


class SourceEnrichedPayload(BaseModel):
    """Payload for a Source-row enrichment write authored by the worker."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["source_enriched"] = "source_enriched"
    aizk_uuid: UUID
    columns_written: list[str]
    update_succeeded: bool
    failure_reason: str | None = None


JobEventPayload = Annotated[
    Union[
        QueuedPayload,
        ClaimedPayload,
        PhasePayload,
        CancelledPayload,
        FailedPayload,
        SucceededPayload,
        UploadPendingPayload,
        RecoveredStalePayload,
        SourceEnrichedPayload,
    ],
    PydField(discriminator="kind"),
]
"""Discriminated union over all event payload variants, keyed by ``kind``."""


_KIND_TO_MODEL: dict[ConversionEventKind, type[BaseModel]] = {
    ConversionEventKind.QUEUED: QueuedPayload,
    ConversionEventKind.CLAIMED: ClaimedPayload,
    ConversionEventKind.PHASE: PhasePayload,
    ConversionEventKind.CANCELLED: CancelledPayload,
    ConversionEventKind.FAILED: FailedPayload,
    ConversionEventKind.SUCCEEDED: SucceededPayload,
    ConversionEventKind.UPLOAD_PENDING: UploadPendingPayload,
    ConversionEventKind.RECOVERED_STALE: RecoveredStalePayload,
    ConversionEventKind.SOURCE_ENRICHED: SourceEnrichedPayload,
}


_payload_adapter: TypeAdapter[JobEventPayload] = TypeAdapter(JobEventPayload)


def parse_payload_lenient(raw: str | dict[str, Any]) -> BaseModel:
    """Deserialize a persisted event payload tolerating unrecognized fields.

    Pre-filters ``raw`` to keep only keys declared on the variant model for
    the row's ``kind`` before invoking ``model_validate``. This is the
    forward-compatible read path described in the module docstring (item 3):
    a row written by a future code version that added an optional field to
    a known kind still parses cleanly under current code.

    An unknown ``kind`` raises ``ValueError`` — the closed enumeration is
    the audit trail and is never forward-tolerant.
    """
    raw_dict: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else dict(raw)
    kind_value = raw_dict.get("kind")
    try:
        kind = ConversionEventKind(kind_value)
    except ValueError as exc:
        raise ValueError(f"Unknown event kind: {kind_value!r}") from exc

    model_cls = _KIND_TO_MODEL[kind]
    known = set(model_cls.model_fields.keys())
    filtered = {k: v for k, v in raw_dict.items() if k in known}
    return model_cls.model_validate(filtered)


# ---------------------------------------------------------------------------
# Work-unit reference + read helper for the shared event log
# ---------------------------------------------------------------------------


def _work_unit_ref(job_id: int | None) -> str:
    """Render a conversion job's id to the generic ``work_unit_ref`` text.

    The shared ``pipeline_events`` table carries each stage's own unit identity
    as text (no per-stage FK). Conversion's unit is the integer
    ``ConversionJob.id``, stored verbatim as its string form so a single job's
    events are retrievable by ``(stage, work_unit_ref)``.
    """
    return str(job_id)


def events_for_job(session: "Session", job_id: int) -> list[PipelineEvent]:
    """Return a conversion job's events from ``pipeline_events`` in order.

    Filters the shared event log to ``stage="conversion"`` and the job's
    ``work_unit_ref`` (its id rendered to text), ordered by primary key so the
    sequence matches insertion/occurrence order. This is the relocation-aware
    replacement for the former ``conversion_job_events`` per-job query.

    Args:
        session: Active session to read through.
        job_id: The conversion job id whose events to return.

    Returns:
        The job's :class:`~aizk.pipeline.events.PipelineEvent` rows in order.
    """
    return list(
        session.exec(
            select(PipelineEvent)
            .where(PipelineEvent.stage == STAGE)
            .where(PipelineEvent.work_unit_ref == _work_unit_ref(job_id))
            .order_by(PipelineEvent.event_id)
        ).all()
    )


# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------


def _serialize_payload(payload: BaseModel, expected_kind: ConversionEventKind) -> str:
    """Validate kind agreement and serialize a payload to JSON.

    Dump first, then validate through the discriminated union. That keeps
    helper callers from smuggling an arbitrary ``BaseModel`` with a matching
    ``kind`` past the strict per-kind payload contract.
    """
    payload_data = payload.model_dump(mode="python")
    payload_kind = payload_data.get("kind")
    try:
        payload_kind_enum = ConversionEventKind(payload_kind)
    except ValueError as exc:
        raise ValueError(f"Unknown payload kind {payload_kind!r}") from exc

    if payload_kind_enum is not expected_kind:
        raise ValueError(f"Payload kind {payload_kind!r} does not match transition kind {expected_kind!r}")

    validated_payload = _payload_adapter.validate_python(payload_data)
    return validated_payload.model_dump_json()


def record_transition(
    session: "Session",
    job: "ConversionJob",
    *,
    to_status: ConversionJobStatus,
    kind: ConversionEventKind,
    attempt: int,
    payload: BaseModel,
    from_status: ConversionJobStatus | None | _UnsetType = _UNSET,
) -> PipelineEvent:
    """Record a status transition and the matching event row in one transaction.

    Default behavior reads ``job.status`` as ``from_status`` *before*
    mutation, mutates the job to ``to_status``, and appends one
    :class:`~aizk.pipeline.events.PipelineEvent` row (``stage="conversion"``)
    carrying the typed ``payload`` serialized into the generic ``payload_json``
    column. The conversion-specific ``kind`` and statuses are rendered to their
    string values; the denormalized ``aizk_uuid`` keeps the row queryable after
    operator deletion of the job (there is no DB FK in the shared table).

    The conversion-specific payload is validated against the strict per-kind
    typed union *before* the job is mutated, so a mismatched or malformed
    payload leaves ``job.status`` untouched.

    For origin events (the first event for a brand-new job), pass
    ``from_status=None`` explicitly. This is required because the API
    submit path constructs the ``ConversionJob`` with ``status=QUEUED``
    already and the spec requires the origin event to carry no prior
    status.

    Does NOT call ``session.commit()`` — the caller's surrounding
    transaction determines commit boundaries. See module docstring (item 1)
    for the rationale.

    Transient jobs: if ``job.id`` is ``None`` (i.e., the job was just
    constructed and not yet flushed), the caller MUST call
    ``session.flush()`` before invoking this helper so the event row's
    ``work_unit_ref`` captures a non-null job id. Calling without a flush
    persists ``work_unit_ref="None"`` and the link to the job is lost.

    Args:
        session: Active SQLModel session. The caller owns commit/rollback.
        job: The ConversionJob whose status is being mutated.
        to_status: New status to apply to the job.
        kind: Event kind for the audit row. Must match ``payload.kind``.
        attempt: Attempt number the event belongs to. REQUIRED.
            See ``design.md § HelperCallingConventions`` for per-site values.
        payload: Typed payload model for the kind.
        from_status: Optional override for the event's prior-status column.
            When omitted, derived from ``job.status`` before mutation.
            Pass ``None`` explicitly to write NULL (origin events).

    Returns:
        The constructed (and ``session.add``-staged) event row.

    Raises:
        ValueError: If ``payload.kind`` does not match ``kind``.
        ValidationError: If ``payload`` does not satisfy the typed payload
            model for its ``kind``.
    """
    # Validate the conversion-specific payload through the strict per-kind union
    # BEFORE any mutation, so a bad payload never leaves a half-applied status.
    payload_json = _serialize_payload(payload, kind)

    if isinstance(from_status, _UnsetType):
        prior_status: ConversionJobStatus | None = job.status
    else:
        prior_status = from_status
    job.status = to_status

    event = PipelineEvent(
        stage=STAGE,
        work_unit_ref=_work_unit_ref(job.id),
        aizk_uuid=job.aizk_uuid,
        attempt=attempt,
        kind=kind.value,
        from_status=prior_status.value if prior_status is not None else None,
        to_status=to_status.value,
        payload_json=payload_json,
    )

    session.add(job)
    session.add(event)
    return event


def record_phase_event(
    session: "Session",
    *,
    job_id: int,
    aizk_uuid: UUID,
    attempt: int,
    current_status: ConversionJobStatus,
    phase: str,
    reported_at: datetime.datetime,
) -> PipelineEvent | None:
    """Record a subprocess-reported phase report as an event row.

    Does NOT mutate any job status — phase reports describe progress within
    the RUNNING state. Both ``from_status`` and ``to_status`` are set to
    ``current_status`` so the row's column shape stays consistent.

    The row lands in the shared ``pipeline_events`` table with
    ``stage="conversion"`` and the job's ``work_unit_ref``.

    Best-effort: validation failure (unrecognized phase, extra field) and
    persistence failure are both logged at WARNING and swallowed. The job's
    real-time control flow is unaffected; only the durable replay record
    is degraded.

    Returns the event row when persistence succeeds, or ``None`` when it
    was dropped due to validation or persistence failure.
    """
    try:
        payload = PhasePayload(phase=phase, reported_at=reported_at)  # type: ignore[arg-type]
    except ValidationError as exc:
        logger.warning(
            "Phase event dropped due to validation failure: job_id=%s attempt=%s phase=%r error=%s",
            job_id,
            attempt,
            phase,
            exc,
        )
        return None

    event = PipelineEvent(
        stage=STAGE,
        work_unit_ref=_work_unit_ref(job_id),
        aizk_uuid=aizk_uuid,
        attempt=attempt,
        kind=ConversionEventKind.PHASE.value,
        from_status=current_status.value,
        to_status=current_status.value,
        payload_json=payload.model_dump_json(),
    )

    try:
        session.add(event)
    except Exception:  # pragma: no cover — defensive; session.add itself rarely raises
        logger.warning(
            "Phase event persistence failed: job_id=%s attempt=%s phase=%r",
            job_id,
            attempt,
            phase,
            exc_info=True,
        )
        return None

    return event


def record_source_event(
    session: "Session",
    *,
    job_id: int,
    aizk_uuid: UUID,
    attempt: int,
    columns_written: list[str],
    update_succeeded: bool,
    failure_reason: str | None = None,
) -> PipelineEvent | None:
    """Record a Source-enrichment write attempt as an event row.

    Emits regardless of whether the Source UPDATE succeeded — the audit is
    of *what the worker attempted and the outcome*, not of the underlying
    mutation's atomicity. Mirrors ``record_phase_event``'s best-effort
    posture: validation or persistence failures are logged and swallowed.

    The row lands in the shared ``pipeline_events`` table with
    ``stage="conversion"`` and the job's ``work_unit_ref``. Both status columns
    are NULL because a source-enrichment write does not assert a target
    ``ConversionJob.status``.

    Returns the event row when persistence is staged, or ``None`` when it
    was dropped.
    """
    try:
        payload = SourceEnrichedPayload(
            aizk_uuid=aizk_uuid,
            columns_written=columns_written,
            update_succeeded=update_succeeded,
            failure_reason=failure_reason,
        )
    except ValidationError as exc:
        logger.warning(
            "Source-enrichment event dropped due to validation failure: job_id=%s attempt=%s columns=%r error=%s",
            job_id,
            attempt,
            columns_written,
            exc,
        )
        return None

    event = PipelineEvent(
        stage=STAGE,
        work_unit_ref=_work_unit_ref(job_id),
        aizk_uuid=aizk_uuid,
        attempt=attempt,
        kind=ConversionEventKind.SOURCE_ENRICHED.value,
        from_status=None,
        to_status=None,
        payload_json=payload.model_dump_json(),
    )

    try:
        session.add(event)
    except Exception:  # pragma: no cover — defensive
        logger.warning(
            "Source-enrichment event persistence failed: job_id=%s attempt=%s",
            job_id,
            attempt,
            exc_info=True,
        )
        return None

    return event
