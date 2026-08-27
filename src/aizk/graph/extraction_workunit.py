"""Enqueue paths for the extraction stage's work-units.

Mirrors :mod:`aizk.graph.workunit`'s enqueue functions for the extraction
stage's :class:`~aizk.graph.datamodel.ExtractionJob` table: an incremental
one-source enqueue and a bulk/backfill enqueue over eligible sources, both
deduped on ``idempotency_key`` so re-enqueueing the same source reuses the
open row. Unlike the contextualization work-unit (which locates its Markdown
by ``conversion_output_id``), an extraction work-unit carries only the durable
``source_id``: :func:`aizk.graph.extraction_run.extract_document` resolves the
source's active chunking run and contextualized variants itself, so no
upstream artifact locator is needed here.

Both paths honor the stage's declared capacity (:mod:`aizk.graph.capacity`):
the single enqueue refuses at capacity, the bulk enqueue truncates to the
batch's headroom.

:func:`pending_extraction_sources` is the stage's pending-work derivation: the
eligible set the bulk enqueue resolves, narrowed to the sources with no work-unit
yet. It is read-only, so admission and the operator surfaces read one derivation.

This surface deduplicates on the source alone and never re-enqueues a
terminal unit — an existing row is reused whatever its status, including
``SUCCEEDED`` (a no-op). It is therefore **not** a re-extraction trigger.
Re-extraction after an upstream-generation change is a requeue of the existing
unit rather than a second row: the identity key stays ``source:{source_id}``,
and :func:`aizk.graph.job_actions.apply_extraction_readmission` transitions a
finished unit back to ``QUEUED`` when
:func:`aizk.graph.extraction_run.stale_extraction_sources` says the source's
upstream has moved on. The worker then re-reads the source's current active
inputs and its run supersedes the prior one. That action is operator-initiated —
no staleness condition re-admits work on its own. Re-extraction after an
extractor, materializer, or input-policy change remains a matter for the direct
entry points (:func:`~aizk.graph.extraction_run.extract_source` /
:func:`~aizk.graph.extraction_run.extract_corpus`), where the run's derivation
key decides reuse versus supersession.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlmodel import select

from aizk.graph.capacity import check_capacity, within_headroom
from aizk.graph.datamodel import ExtractionJob
from aizk.graph.extraction_events import EXTRACTION_STAGE
from aizk.graph.persistence import CHUNKING_STAGE
from aizk.pipeline.invalidation import require_reprocessing_confirmation
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlmodel import Session

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc)


def _idempotency_key(source_id: UUID) -> str:
    """Return the enqueue-dedupe key for a source.

    Keyed by the durable source identity alone — unlike contextualization's
    per-artifact key — so a re-enqueue of the same source always targets the
    same work-unit row, whatever its status. Extraction reads whichever
    chunking/contextualization generation is active for the source at execute
    time; an upstream-generation change does not rotate this key, because
    re-extraction requeues the existing unit (see the module docstring).
    """
    return f"source:{source_id}"


def enqueue_extraction(session: "Session", *, source_id: UUID, queue_max_depth: int) -> ExtractionJob:
    """Enqueue one source's extraction work-unit (incremental mode), deduped on ``idempotency_key``.

    If a work-unit for this source already exists — in **any** status,
    including a terminal one — it is returned unchanged rather than
    duplicated or re-queued: an incremental re-ingest or an overlapping
    backfill reuses the row, and re-enqueueing an already-``SUCCEEDED``
    source is a no-op (this function is not a re-extraction trigger; see the
    module docstring). Otherwise a new ``QUEUED`` unit is inserted and
    flushed (so its ``id`` is available).

    The capacity check runs **after** the dedupe branch, so a request that resolves
    to an existing unit is never refused. This is the only place extraction
    work-unit rows are constructed, so the limit binds every caller.

    Does **not** commit; the caller owns the surrounding transaction.

    Args:
        session: Active session; the caller owns commit/rollback.
        source_id: The durable source identity to extract.
        queue_max_depth: The stage's declared capacity over its actionable
            backlog; ``0`` declares no limit, and every caller must say which it means.

    Returns:
        The existing or newly-created :class:`~aizk.graph.datamodel.ExtractionJob`.

    Raises:
        StageAtCapacityError: When the backlog is at or above ``queue_max_depth``
            and the request does not resolve to an existing unit.
    """
    key = _idempotency_key(source_id)
    existing = session.exec(select(ExtractionJob).where(ExtractionJob.idempotency_key == key)).one_or_none()
    if existing is not None:
        logger.debug("Reusing extraction work-unit id=%s for source_id=%s", existing.id, source_id)
        return existing

    check_capacity(session, ExtractionJob, stage=EXTRACTION_STAGE, limit=queue_max_depth)

    job = ExtractionJob(
        idempotency_key=key,
        source_id=source_id,
        status=WorkUnitStatus.QUEUED,
        queued_at=_utcnow(),
    )
    session.add(job)
    session.flush()
    logger.debug("Enqueued extraction work-unit id=%s for source_id=%s", job.id, source_id)
    return job


def _sources_with_active_chunking_run(session: "Session") -> list[UUID]:
    """Return the source_ids with an active chunking run, in run-creation order.

    A source with no chunking run has nothing to extract
    (``extract_document`` rejects it), so the backfill enqueue scopes itself
    to sources that are actually eligible rather than enqueueing units doomed
    to a permanent failure.
    """
    scope_ids = session.exec(
        select(PipelineRun.scope_id)
        .where(PipelineRun.stage == CHUNKING_STAGE, PipelineRun.status == RunStatus.ACTIVE)
        .order_by(PipelineRun.created_at)
    ).all()
    return [UUID(scope_id) for scope_id in scope_ids]


def _enqueued_sources(session: "Session", source_ids: "Sequence[UUID]") -> set[UUID]:
    """Return which of ``source_ids`` already have an extraction work-unit."""
    if not source_ids:
        return set()
    return set(session.exec(select(ExtractionJob.source_id).where(ExtractionJob.source_id.in_(source_ids))).all())


def pending_extraction_sources(session: "Session", *, limit: int | None = None) -> list[UUID]:
    """Return the sources that should have an extraction work-unit but do not.

    A source is pending exactly when it has an active chunking run and no
    extraction work-unit. Because the work-unit is keyed by the source alone, a
    source that already has one is never pending, whatever that unit's status: a
    succeeded unit whose chunking has since been superseded is stale, not pending.

    Derived from current run and work-unit state alone — nothing records that a
    previous evaluation saw a source, so a source this evaluation leaves out is
    still pending for the next one.

    The anti-join resolves in Python rather than SQL: a work-unit carries a UUID
    ``source_id`` and a run carries the string ``scope_id``, so normalizing them in
    SQL would depend on how a backend serializes a UUID. This is the boundary
    conversion the stage applies everywhere else.

    Args:
        session: Active, read-only session.
        limit: Caps the result to its first N pending sources when supplied. The
            bound applies after the anti-join, so a bounded evaluation returns
            pending work rather than a sample of the corpus that may hold none.
            Must be one or more.

    Returns:
        The pending source identities, in chunking-run creation order.

    Raises:
        ValueError: If ``limit`` is supplied and is less than one.
    """
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit}")
    enqueued = set(session.exec(select(ExtractionJob.source_id)).all())
    pending = [source_id for source_id in _sources_with_active_chunking_run(session) if source_id not in enqueued]
    return pending[:limit] if limit is not None else pending


def enqueue_extraction_backfill(
    session: "Session",
    *,
    confirmed: bool = False,
    queue_max_depth: int,
) -> list[ExtractionJob]:
    """Enqueue extraction work-units for every eligible source (bulk/backfill mode).

    Eligible sources are those with an active chunking run (see
    :func:`_sources_with_active_chunking_run`). Each is enqueued via
    :func:`enqueue_extraction`, so the same ``idempotency_key`` dedupe applies
    and the resulting units are identical to incremental enqueue — only volume
    and scheduling differ.

    Capacity is read once for the batch and the eligible set truncated to it.
    Sources beyond the headroom stay eligible for a later batch.

    A corpus-wide backfill has a large downstream blast radius, so it is gated
    behind explicit confirmation: nothing is enqueued unless ``confirmed`` is
    ``True`` (see :func:`aizk.pipeline.invalidation.require_reprocessing_confirmation`).

    Args:
        session: Active session; the caller owns commit/rollback.
        confirmed: Explicit human approval for the corpus-wide operation; when
            ``False`` (the default) nothing is enqueued and the gate raises.
        queue_max_depth: The stage's declared capacity over its actionable
            backlog; ``0`` declares no limit, and every caller must say which it means.

    Returns:
        The enqueued (or reused) work-units, one per admitted source.

    Raises:
        ReprocessingConfirmationError: When ``confirmed`` is ``False``.
    """
    require_reprocessing_confirmation("corpus-wide extraction backfill", confirmed=confirmed)
    eligible = _sources_with_active_chunking_run(session)
    admitted = within_headroom(
        session,
        ExtractionJob,
        eligible,
        stage=EXTRACTION_STAGE,
        limit=queue_max_depth,
        already_enqueued=_enqueued_sources(session, eligible),
    )
    return [
        enqueue_extraction(session, source_id=source_id, queue_max_depth=queue_max_depth) for source_id in admitted
    ]
