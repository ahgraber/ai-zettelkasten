"""The stage-run / dataset-version primitive.

A :class:`PipelineRun` records a stage's derived-output generation keyed by
``(stage, scope_id)``. The stage defines its own ``scope_id`` (per-document,
per-chunk, corpus-wide). At most one run per ``(stage, scope_id)`` is active,
enforced by a partial unique index; :func:`record_run` activates a new run and
supersedes the prior one atomically in the caller's transaction, so there is
never more than one active run nor a window with none.

The primitive is independent of work-unit execution — a stage may record runs
without using the runner, and vice versa. It does not own a stage's derived
output rows; superseding is expressed purely as a status transition from
``active`` to ``superseded``, leaving prior-run outputs untouched.

Three helpers share the run's shape across stages: :func:`active_run` looks up
the active run of a scope, :func:`require_active_run` loads a run by id and
refuses one that is missing, superseded, or of another stage, and
:func:`record_applicability` is the idempotent insert of a stage's applicability
relation (an existing row is returned unchanged; a conflicting one is refused).
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from sqlalchemy import Column, DateTime, Enum as SAEnum, Index, Text, func, text
from sqlmodel import Field, SQLModel, select

from aizk.utilities.clock import utcnow
from aizk.utilities.hashing import canonical_json

if TYPE_CHECKING:
    from sqlmodel import Session

ApplicabilityModel = TypeVar("ApplicabilityModel", bound=SQLModel)


class RunStatus(str, Enum):
    """Lifecycle status of a stage run."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


class PipelineRun(SQLModel, table=True):
    """A stage's derived-output generation, scoped by ``(stage, scope_id)``.

    ``scope_id`` is a UUID identity: every real scope is a source UUID, and a
    stage whose scope is not naturally a UUID mints one (a ``uuid5`` namespace
    identity) rather than storing a non-UUID string. Rows are immutable once
    recorded except for the ``active`` → ``superseded`` status transition.
    ``supersedes_run_id`` is a logical reference to the run this one replaced
    (no database foreign key, so superseded-run compaction can delete freely).
    ``version_stamps_json`` holds an opaque JSON object of reproducibility
    version identifiers the stage records; the primitive does not interpret it.
    """

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        # At most one active run per (stage, scope_id). Partial unique index:
        # superseded rows are exempt, so history accumulates without conflict.
        Index(
            "uq_pipeline_runs_active_scope",
            "stage",
            "scope_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_pipeline_runs_stage_scope_id", "stage", "scope_id"),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    stage: str = Field(nullable=False)
    scope_id: UUID = Field(nullable=False)
    # values_callable stores enum values ("active") not names ("ACTIVE"), matching the index predicate.
    status: RunStatus = Field(
        default=RunStatus.ACTIVE,
        sa_column=Column(
            SAEnum(RunStatus, values_callable=lambda x: [e.value for e in x]),
            nullable=False,
        ),
    )
    derivation_key: str = Field(sa_column=Column(Text, nullable=False))
    version_stamps_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    supersedes_run_id: int | None = Field(default=None, nullable=True)
    created_at: datetime.datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(),
            nullable=False,
            server_default=func.current_timestamp(),
        ),
    )


def record_run(
    session: "Session",
    *,
    stage: str,
    scope_id: UUID,
    derivation_key: str,
    version_stamps: dict[str, str] | None = None,
) -> PipelineRun:
    """Activate a new run and supersede the prior active one atomically.

    Within the caller's transaction: demote the current active run for
    ``(stage, scope_id)`` to ``superseded`` (if any), then insert the new run
    as ``active`` carrying ``supersedes_run_id`` of the demoted run. The
    intermediate ``session.flush()`` orders the demote before the insert so the
    partial unique index never sees two active rows.

    Does **not** commit — the caller's surrounding transaction (a
    ``BEGIN IMMEDIATE`` block under the single serialized writer) determines
    commit boundaries, mirroring ``record_transition``. A flush surfaces a
    partial-unique violation inside the caller's transaction; on rollback,
    neither the demotion nor the new run persists.

    Args:
        session: Active session; the caller owns commit/rollback.
        stage: The stage that owns this run.
        scope_id: The stage-defined scope the run's outputs belong to.
        derivation_key: Deterministic key for the inputs/configuration that
            produced the run's derived outputs.
        version_stamps: Optional reproducibility version identifiers; stored as
            a deterministic JSON object and not interpreted by the primitive.

    Returns:
        The newly-activated :class:`PipelineRun` (``session.add``-staged).
    """
    prior = active_run(session, stage, scope_id)

    supersedes_run_id: int | None = None
    if prior is not None:
        prior.status = RunStatus.SUPERSEDED
        session.add(prior)
        # Demote before inserting so the partial unique index sees one active row.
        session.flush()
        supersedes_run_id = prior.id

    new_run = PipelineRun(
        stage=stage,
        scope_id=scope_id,
        status=RunStatus.ACTIVE,
        derivation_key=derivation_key,
        version_stamps_json=canonical_json(version_stamps or {}, ensure_ascii=True),
        supersedes_run_id=supersedes_run_id,
    )
    session.add(new_run)
    # Surface a partial-unique violation here, inside the caller's transaction.
    session.flush()
    return new_run


def reuse_or_record_run(
    session: "Session",
    *,
    stage: str,
    scope_id: UUID,
    derivation_key: str,
    version_stamps: dict[str, str] | None = None,
) -> PipelineRun:
    """Reuse the active run when its derivation key matches, else record a new one.

    The run-level idempotency primitive: re-invoking a stage for a
    ``(stage, scope_id)`` whose ``derivation_key`` equals the active run's
    returns that run unchanged — no new run, no supersession, no duplicate
    outputs — so a retry or re-ingestion with unchanged inputs is a no-op at the
    run level. A differing (or absent) active key records a new active run that
    supersedes the prior one via :func:`record_run`.

    A stage with a stricter reuse condition than derivation-key equality (for
    example also requiring its output rows to be present and complete) keeps that
    check at its own layer and calls :func:`record_run` directly; this primitive
    is the floor every stage shares.

    Does **not** commit — the caller owns the surrounding transaction.

    Args:
        session: Active session; the caller owns commit/rollback.
        stage: The stage that owns this run.
        scope_id: The stage-defined scope the run's outputs belong to.
        derivation_key: Deterministic key for the inputs/configuration that
            produced the run's derived outputs; compared against the active run's.
        version_stamps: Optional reproducibility version identifiers, used only
            when a new run is recorded.

    Returns:
        The reused active :class:`PipelineRun`, or the newly-activated one.
    """
    active = active_run(session, stage, scope_id)
    if active is not None and active.derivation_key == derivation_key:
        return active
    return record_run(
        session,
        stage=stage,
        scope_id=scope_id,
        derivation_key=derivation_key,
        version_stamps=version_stamps,
    )


def active_run(session: "Session", stage: str, scope_id: UUID) -> PipelineRun | None:
    """Return the active run for ``(stage, scope_id)``, or ``None`` when the scope has none.

    The partial unique index guarantees at most one such row, so the lookup is
    ``one_or_none``.
    """
    return session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == stage,
            PipelineRun.scope_id == scope_id,
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one_or_none()


def require_active_run(session: "Session", run_id: int, stage: str, *, label: str) -> PipelineRun:
    """Load run ``run_id`` and require it to be an active run of ``stage``.

    Args:
        session: Active session.
        run_id: Primary key of the run to load.
        stage: The stage the run must belong to.
        label: The stage's human name for the error message (for example
            ``"chunking run"``).

    Returns:
        The active :class:`PipelineRun`.

    Raises:
        ValueError: If no run has that id, the run is not ``active``, or it
            belongs to another stage.
    """
    run = session.get(PipelineRun, run_id)
    if run is None or run.status != RunStatus.ACTIVE or run.stage != stage:
        raise ValueError(f"{label} {run_id!r} is not an active {label}: it is missing, not active, or not a {label}")
    return run


def record_applicability(
    session: "Session",
    model: type[ApplicabilityModel],
    key: dict[str, Any],
    *,
    input_fingerprint: str,
    **fields: Any,
) -> ApplicabilityModel:
    """Stage an applicability row idempotently; the caller commits.

    An applicability relation records that a run's outputs apply to an input
    other than the one the run consumed. The relation is append-only: a row that
    already exists under ``key`` is returned unchanged when its
    ``input_fingerprint`` and every value in ``fields`` match, and refused when
    any of them differs, because there is no update path.

    Args:
        session: Active session; the caller owns commit/rollback.
        model: The stage's applicability table.
        key: The composite primary key, keyed by column name.
        input_fingerprint: Fingerprint of the input the run is declared to apply to.
        **fields: Further columns of the row, compared on an existing row and
            written on a new one.

    Returns:
        The existing or newly ``session.add``-staged row.

    Raises:
        ValueError: If a row exists under ``key`` with a different
            ``input_fingerprint`` or a different value for any of ``fields``.
    """
    existing = session.get(model, key)
    if existing is not None:
        conflicts = existing.input_fingerprint != input_fingerprint or any(
            getattr(existing, name) != value for name, value in fields.items()
        )
        if conflicts:
            raise ValueError(
                f"{model.__tablename__} row {key!r} is already recorded with a different input_fingerprint "
                "or field value; there is no update path"
            )
        return existing

    row = model(**key, input_fingerprint=input_fingerprint, **fields)
    session.add(row)
    return row
