"""Lazy invalidation: staleness detection and the large-reprocessing confirmation gate.

The two halves of the pipeline-identity rule that invalidation is lazy by default
and gates large reprocessing behind explicit confirmation (see
:mod:`aizk.pipeline.identity`):

- **Staleness detection** compares a generation's recorded producer version to the
  current one. It is a cheap, read-only flag that never recomputes, so a producer
  version bump leaves the prior generation active and usable until a later
  re-derivation supersedes it — lazily, on access or through an explicit
  operation, never eagerly.
- **The confirmation gate** is the surface-agnostic checkpoint a reprocessing
  entry point calls before a corpus-wide or cascading operation runs, so such an
  operation does not run until a human explicitly approves it.

Both operate on the shared run primitive (:mod:`aizk.pipeline.run`): a generation
is the cohort of outputs under one active run for a ``(stage, scope_id)``, and the
producer version it was produced under is recorded in the run's version stamps.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlmodel import select

from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from sqlmodel import Session


def generation_is_stale(run: PipelineRun, *, version_field: str, current_version: int | str) -> bool:
    """Return whether a generation's recorded producer version is older than the current one.

    A **logical** staleness flag only: it compares the producer version the run
    recorded in its version stamps (under ``version_field``) against
    ``current_version`` and never triggers recompute. A stale generation stays
    active and usable; recompute is a separate, deliberate action — a re-derivation
    whose version-bearing derivation key supersedes it. Returns ``False`` when the
    run recorded no such version stamp (nothing to compare).

    Args:
        run: The generation's run.
        version_field: The version-stamp key naming the producer version (e.g.
            ``"splitter_version"``, ``"summary_version"``, ``"context_version"``).
        current_version: The producer's current version constant.

    Returns:
        ``True`` iff the run recorded a version under ``version_field`` that
        differs from ``current_version``.
    """
    recorded = json.loads(run.version_stamps_json).get(version_field)
    return recorded is not None and str(recorded) != str(current_version)


def stale_active_generations(
    session: "Session",
    *,
    stage: str,
    version_field: str,
    current_version: int | str,
) -> list[PipelineRun]:
    """Return the active generations for ``stage`` produced under an older producer version.

    Read-only detection: it identifies logically-stale active runs by comparing
    each one's recorded producer version to ``current_version`` (via
    :func:`generation_is_stale`) and recomputes nothing. A caller decides whether
    and when to re-derive each stale generation; a version-heterogeneous corpus is
    valid in the meantime.
    """
    actives = session.exec(
        select(PipelineRun).where(PipelineRun.stage == stage, PipelineRun.status == RunStatus.ACTIVE)
    ).all()
    return [
        run
        for run in actives
        if generation_is_stale(run, version_field=version_field, current_version=current_version)
    ]


class ReprocessingConfirmationError(RuntimeError):
    """A large-blast-radius reprocessing operation was requested without explicit confirmation.

    Raised by :func:`require_reprocessing_confirmation` so any surface (CLI, API,
    operator tool) can catch it, warn the user, and re-invoke with approval. It
    carries a human-readable warning and computes no cost.
    """


def require_reprocessing_confirmation(operation: str, *, confirmed: bool) -> None:
    """Gate a large-blast-radius reprocessing operation behind explicit human confirmation.

    Surface-agnostic: any entry point that initiates corpus-wide reprocessing (a
    backfill over many sources) or a base-document edit that cascades through the
    derivation graph calls this before doing the work. Unless ``confirmed`` is
    ``True``, it raises :class:`ReprocessingConfirmationError` with a warning
    that approval is required, so the operation does not run until a human approves
    it. The gate warns and requires approval only; it computes no cost.

    Args:
        operation: A human-readable description of the gated operation, surfaced in
            the warning a caller shows the user.
        confirmed: The caller's explicit approval, gathered however the surface
            sees fit. ``False`` (the default a safe entry point passes through)
            refuses; ``True`` proceeds.

    Raises:
        ReprocessingConfirmationError: When ``confirmed`` is ``False``.
    """
    if not confirmed:
        raise ReprocessingConfirmationError(
            f"{operation} has a large downstream blast radius and will not run until it is "
            "explicitly confirmed; re-invoke with confirmation to approve."
        )
