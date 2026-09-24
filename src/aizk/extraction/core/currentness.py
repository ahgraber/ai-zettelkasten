"""Classify the currentness of each source's active extraction run."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

from sqlmodel import select

from aizk.graph.chunks import CHUNKING_STAGE
from aizk.graph.datamodel import (
    ChunkRunInput,
    ContextualizationRunInput,
    ExtractionRunApplicability,
    ExtractionRunInput,
)
from aizk.graph.mentions import INPUT_POLICY_CONTEXTUALIZED, MENTION_EXTRACTION_STAGE, InputPolicy
from aizk.graph.variants import VARIANT_STAGE
from aizk.pipeline.run import PipelineRun, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlmodel import Session

    from aizk.graph.mentions import ResolvedExtractionConfig


class Currentness(str, Enum):
    """Freshness of a source's active extraction run.

    ``CURRENT`` matches resolved provenance. ``NEEDS_RECONCILIATION`` has the same
    payload but lacks applicability for the resolved upstream run. ``STALE`` has a
    changed payload or configuration.
    """

    CURRENT = "current"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    STALE = "stale"


def _resolve_upstream_run(
    session: "Session",
    *,
    source_id: UUID,
    input_policy: InputPolicy,
    chunking_run: PipelineRun,
) -> PipelineRun:
    """Resolve the upstream run used by extraction.

    Raw input always uses the active chunking run. Contextualized input uses the
    active variant run when present, and otherwise uses the chunking run.
    """
    if input_policy == INPUT_POLICY_CONTEXTUALIZED:
        variant_run = session.exec(
            select(PipelineRun).where(
                PipelineRun.stage == VARIANT_STAGE,
                PipelineRun.scope_id == source_id,
                PipelineRun.status == RunStatus.ACTIVE,
            )
        ).one_or_none()
        if variant_run is not None:
            return variant_run
    return chunking_run


def _upstream_output_fingerprint(session: "Session", upstream_run: "PipelineRun") -> str:
    """Return the persisted output fingerprint for a chunking or variant run.

    The function reads the producing stage's provenance row instead of
    recomputing the fingerprint.

    Raises:
        ValueError: If the stage is unsupported or its provenance row is missing.
    """
    assert upstream_run.id is not None  # noqa: S101 — an active run is always persisted
    if upstream_run.stage == CHUNKING_STAGE:
        chunk_input = session.get(ChunkRunInput, upstream_run.id)
        if chunk_input is None:
            raise ValueError(f"chunking run {upstream_run.id!r} has no recorded ChunkRunInput")
        return chunk_input.chunk_set_fingerprint
    if upstream_run.stage == VARIANT_STAGE:
        context_input = session.get(ContextualizationRunInput, upstream_run.id)
        if context_input is None:
            raise ValueError(f"contextualization run {upstream_run.id!r} has no recorded ContextualizationRunInput")
        return context_input.output_fingerprint
    raise ValueError(
        f"run {upstream_run.id!r} has stage {upstream_run.stage!r}, expected a chunking or contextualization run"
    )


def _classify_extraction(
    session: "Session", *, resolved: "ResolvedExtractionConfig", scope_ids: "Sequence[UUID] | None" = None
) -> dict[UUID, "Currentness"]:
    """Classify active extraction runs against resolved upstream and configuration.

    The query count is fixed for the selected scope, and the function does not
    load chunk or variant text.
    """
    config_fingerprint = resolved.config_fingerprint()

    extraction_query = (
        select(PipelineRun, ExtractionRunInput)
        .join(ExtractionRunInput, ExtractionRunInput.run_id == PipelineRun.id)
        .where(PipelineRun.stage == MENTION_EXTRACTION_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    )
    if scope_ids is not None:
        extraction_query = extraction_query.where(PipelineRun.scope_id.in_(scope_ids))
    extraction_rows = session.exec(extraction_query).all()
    if not extraction_rows:
        return {}

    extraction_scope_ids = [run.scope_id for run, _ in extraction_rows]

    chunking_rows = session.exec(
        select(PipelineRun.id, PipelineRun.scope_id, ChunkRunInput.chunk_set_fingerprint)
        .join(ChunkRunInput, ChunkRunInput.run_id == PipelineRun.id)
        .where(
            PipelineRun.stage == CHUNKING_STAGE,
            PipelineRun.status == RunStatus.ACTIVE,
            PipelineRun.scope_id.in_(extraction_scope_ids),
        )
    ).all()
    chunking_upstream: dict[UUID, tuple[int, str]] = {
        scope_id: (run_id, output_fingerprint) for run_id, scope_id, output_fingerprint in chunking_rows
    }

    variant_upstream: dict[UUID, tuple[int, str]] = {}
    if resolved.input_policy == INPUT_POLICY_CONTEXTUALIZED:
        variant_rows = session.exec(
            select(PipelineRun.id, PipelineRun.scope_id, ContextualizationRunInput.output_fingerprint)
            .join(ContextualizationRunInput, ContextualizationRunInput.run_id == PipelineRun.id)
            .where(
                PipelineRun.stage == VARIANT_STAGE,
                PipelineRun.status == RunStatus.ACTIVE,
                PipelineRun.scope_id.in_(extraction_scope_ids),
            )
        ).all()
        variant_upstream = {
            scope_id: (run_id, output_fingerprint) for run_id, scope_id, output_fingerprint in variant_rows
        }

    extraction_run_ids = [run.id for run, _ in extraction_rows]
    applicability_rows = session.exec(
        select(ExtractionRunApplicability.run_id, ExtractionRunApplicability.upstream_run_id).where(
            ExtractionRunApplicability.run_id.in_(extraction_run_ids)
        )
    ).all()
    applicable_upstreams: dict[int, set[int]] = {}
    for run_id, upstream_run_id in applicability_rows:
        applicable_upstreams.setdefault(run_id, set()).add(upstream_run_id)

    result: dict[UUID, Currentness] = {}
    for run, run_input in extraction_rows:
        upstream = variant_upstream.get(run.scope_id) or chunking_upstream.get(run.scope_id)
        if upstream is None:
            # No active chunking run for the source: nothing to classify against.
            continue
        upstream_run_id, upstream_output_fingerprint = upstream
        if (
            upstream_output_fingerprint != run_input.input_fingerprint
            or config_fingerprint != run_input.config_fingerprint
        ):
            result[run.scope_id] = Currentness.STALE
        elif run_input.upstream_run_id == upstream_run_id or upstream_run_id in applicable_upstreams.get(
            run.id, set()
        ):
            result[run.scope_id] = Currentness.CURRENT
        else:
            result[run.scope_id] = Currentness.NEEDS_RECONCILIATION
    return result


def classify_extraction_sources(
    session: "Session", *, resolved: "ResolvedExtractionConfig"
) -> dict[UUID, Currentness]:
    """Classify sources with active extraction runs.

    Sources without active chunking or extraction runs are absent. The mapping
    contains each classified source's ``scope_id`` and :class:`Currentness`.

    Args:
        session: Active, read-only session.
        resolved: The currently-resolved extraction configuration.

    Returns:
        The classification mapping.
    """
    return _classify_extraction(session, resolved=resolved)


def classify_extraction_source(
    session: "Session", source_id: UUID, *, resolved: "ResolvedExtractionConfig"
) -> Currentness | None:
    """Classify one source's active extraction run.

    Return ``None`` when no active chunking or extraction run exists. This
    source-scoped call uses a bounded query set.

    Args:
        session: Active, read-only session.
        source_id: The source's identity.
        resolved: The currently-resolved extraction configuration.
    """
    return _classify_extraction(session, resolved=resolved, scope_ids=[source_id]).get(source_id)


def stale_extraction_sources(session: "Session", *, resolved: "ResolvedExtractionConfig") -> set[UUID]:
    """Return source IDs whose active extraction run is ``STALE``.

    Stale sources are not admitted automatically. Sources in
    ``NEEDS_RECONCILIATION`` are excluded.

    Args:
        session: Active, read-only session.
        resolved: The currently-resolved extraction configuration.

    Returns:
        The stale source IDs.
    """
    return {
        source_id
        for source_id, currentness in classify_extraction_sources(session, resolved=resolved).items()
        if currentness is Currentness.STALE
    }
