"""The stage-run / dataset-version primitive.

A :class:`PipelineRun` records a stage's derived-output generation keyed by
``(stage, scope_key)``. The stage defines its own ``scope_key`` (per-document,
per-chunk, corpus-wide). At most one run per ``(stage, scope_key)`` is active,
enforced by a partial unique index; :func:`record_run` activates a new run and
supersedes the prior one atomically in the caller's transaction, so there is
never more than one active run nor a window with none.

The primitive is independent of work-unit execution — a stage may record runs
without using the runner, and vice versa. It does not own a stage's derived
output rows; superseding is expressed purely as a status transition from
``active`` to ``superseded``, leaving prior-run outputs untouched.
"""

from __future__ import annotations

import datetime
from enum import Enum
import json
from typing import TYPE_CHECKING

from sqlalchemy import Column, DateTime, Enum as SAEnum, Index, Text, func, text
from sqlmodel import Field, SQLModel, select

if TYPE_CHECKING:
    from sqlmodel import Session


def _utcnow() -> datetime.datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc)


class RunStatus(str, Enum):
    """Lifecycle status of a stage run."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


class PipelineRun(SQLModel, table=True):
    """A stage's derived-output generation, scoped by ``(stage, scope_key)``.

    Rows are immutable once recorded except for the ``active`` → ``superseded``
    status transition. ``supersedes_run_id`` is a logical reference to the run
    this one replaced (no database foreign key, so superseded-run compaction
    can delete freely). ``version_stamps_json`` holds an opaque JSON object of
    reproducibility version identifiers the stage records; the primitive does
    not interpret it.
    """

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        # At most one active run per (stage, scope_key). Partial unique index:
        # superseded rows are exempt, so history accumulates without conflict.
        Index(
            "uq_pipeline_runs_active_scope",
            "stage",
            "scope_key",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_pipeline_runs_stage_scope_key", "stage", "scope_key"),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    stage: str = Field(nullable=False)
    scope_key: str = Field(nullable=False)
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
        default_factory=_utcnow,
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
    scope_key: str,
    derivation_key: str,
    version_stamps: dict[str, str] | None = None,
) -> PipelineRun:
    """Activate a new run and supersede the prior active one atomically.

    Within the caller's transaction: demote the current active run for
    ``(stage, scope_key)`` to ``superseded`` (if any), then insert the new run
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
        scope_key: The stage-defined scope the run's outputs belong to.
        derivation_key: Deterministic key for the inputs/configuration that
            produced the run's derived outputs.
        version_stamps: Optional reproducibility version identifiers; stored as
            a deterministic JSON object and not interpreted by the primitive.

    Returns:
        The newly-activated :class:`PipelineRun` (``session.add``-staged).
    """
    prior = session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == stage,
            PipelineRun.scope_key == scope_key,
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one_or_none()

    supersedes_run_id: int | None = None
    if prior is not None:
        prior.status = RunStatus.SUPERSEDED
        session.add(prior)
        # Demote before inserting so the partial unique index sees one active row.
        session.flush()
        supersedes_run_id = prior.id

    new_run = PipelineRun(
        stage=stage,
        scope_key=scope_key,
        status=RunStatus.ACTIVE,
        derivation_key=derivation_key,
        version_stamps_json=json.dumps(version_stamps or {}, sort_keys=True, separators=(",", ":")),
        supersedes_run_id=supersedes_run_id,
    )
    session.add(new_run)
    # Surface a partial-unique violation here, inside the caller's transaction.
    session.flush()
    return new_run
