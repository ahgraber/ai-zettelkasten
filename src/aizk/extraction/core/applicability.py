"""Record an extraction run's applicability to an upstream run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aizk.extraction.core.currentness import _upstream_output_fingerprint
from aizk.graph.chunks import CHUNKING_STAGE
from aizk.graph.datamodel import ExtractionRunApplicability, ExtractionRunInput
from aizk.graph.mentions import MENTION_EXTRACTION_STAGE, InputPolicy, extraction_derivation_key
from aizk.graph.variants import VARIANT_STAGE
from aizk.pipeline.run import PipelineRun, record_applicability, require_active_run

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.graph.mentions import ResolvedExtractionConfig


def record_extraction_run_applicability(
    session: "Session",
    *,
    run_id: int,
    upstream_run_id: int,
    input_policy: InputPolicy,
    resolved: "ResolvedExtractionConfig",
) -> ExtractionRunApplicability:
    """Record applicability of an extraction run to an upstream run.

    The upstream fingerprint is read from persisted provenance. The relation is
    append-only and idempotent; an existing identical relation is returned. The
    function does not commit.

    Raises:
        ValueError: If either run is invalid, provenance is missing, the
            derivation key does not match, or an existing relation conflicts.
    """
    run = require_active_run(session, run_id, MENTION_EXTRACTION_STAGE, label="extraction run")

    upstream = session.get(PipelineRun, upstream_run_id)
    if upstream is None or upstream.stage not in (CHUNKING_STAGE, VARIANT_STAGE) or upstream.scope_id != run.scope_id:
        raise ValueError(
            f"upstream run {upstream_run_id!r} does not resolve to a chunking or contextualization run "
            f"sharing scope_id={run.scope_id!r}"
        )

    upstream_output_fingerprint = _upstream_output_fingerprint(session, upstream)
    candidate_key = extraction_derivation_key(
        input_fingerprint=upstream_output_fingerprint, config_fingerprint=resolved.config_fingerprint()
    )
    if candidate_key != run.derivation_key:
        raise ValueError(
            f"the candidate key computed from upstream run {upstream_run_id!r}'s output and the "
            f"currently-resolved configuration does not match run {run_id!r}'s derivation_key"
        )

    run_input = session.get(ExtractionRunInput, run_id)
    if run_input is None:
        raise ValueError(f"run {run_id!r} has no recorded ExtractionRunInput; its persist is not complete")

    return record_applicability(
        session,
        ExtractionRunApplicability,
        {"run_id": run_id, "upstream_run_id": upstream_run_id},
        input_fingerprint=upstream_output_fingerprint,
        input_policy=input_policy,
    )
