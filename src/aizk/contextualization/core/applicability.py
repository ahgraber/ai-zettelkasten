"""Record summary-run and variant-run applicability relations."""

from __future__ import annotations

from sqlmodel import Session, select

from aizk.ai.providers.llm import ModelConfig
from aizk.contextualization.core.keys import (
    contextualization_config_fingerprint,
    contextualization_run_input_fingerprint,
    summary_config_fingerprint,
)
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph.chunks import CHUNKING_STAGE, document_order_chunks, manifest_of_run, run_input
from aizk.graph.datamodel import (
    ChunkRunApplicability,
    ContextualizationRunApplicability,
    ContextualizedChunk,
    DocumentSummary,
    SummaryRunApplicability,
)
from aizk.graph.fingerprints import chunk_key
from aizk.graph.variants import SUMMARY_STAGE, VARIANT_STAGE
from aizk.pipeline.identity import derivation_key
from aizk.pipeline.run import PipelineRun, record_applicability, require_active_run


def _conversion_output(session: "Session", conversion_output_id: str) -> ConversionOutput | None:
    """Resolve a stringified conversion-output locator to its row, or ``None`` if absent or unparsable."""
    try:
        native_id = int(conversion_output_id)
    except ValueError:
        return None
    return session.get(ConversionOutput, native_id)


def record_summary_run_applicability(
    session: "Session",
    *,
    run_id: int,
    conversion_output_id: str,
    input_fingerprint: str,
    model_config: ModelConfig,
    summary_version: int,
) -> SummaryRunApplicability:
    """Validate and stage a summary-run applicability relation; the caller commits.

    The relation is append-only and idempotent on ``(run_id, conversion_output_id)``.
    Existing rows are returned unchanged.

    Raises:
        ValueError: If the run, conversion output, derivation key, summary, or
            existing relation is inconsistent.
    """
    run = require_active_run(session, run_id, SUMMARY_STAGE, label="summary run")

    output = _conversion_output(session, conversion_output_id)
    if output is None or output.source_id != run.scope_id:
        raise ValueError(
            f"conversion output {conversion_output_id!r} does not exist or does not belong to source {run.scope_id!r}"
        )

    candidate_key = derivation_key(
        inputs={"input": input_fingerprint, "config": summary_config_fingerprint(model_config, summary_version)}
    )
    if candidate_key != run.derivation_key:
        raise ValueError(f"summary run {run_id!r}'s derivation key does not match the recomputed candidate key")

    if session.exec(select(DocumentSummary).where(DocumentSummary.run_id == run_id)).first() is None:
        raise ValueError(f"summary run {run_id!r} has no recorded DocumentSummary (a torn run)")

    return record_applicability(
        session,
        SummaryRunApplicability,
        {"run_id": run_id, "conversion_output_id": conversion_output_id},
        input_fingerprint=input_fingerprint,
    )


def _conversion_outputs_of_summary_run(session: "Session", summary_run_id: int) -> set[str]:
    """Return the conversion output ids a summary run is current for: its own plus any applicability rows."""
    summary = session.exec(select(DocumentSummary).where(DocumentSummary.run_id == summary_run_id)).first()
    outputs = {summary.conversion_output_id} if summary is not None else set()
    outputs.update(
        session.exec(
            select(SummaryRunApplicability.conversion_output_id).where(
                SummaryRunApplicability.run_id == summary_run_id
            )
        ).all()
    )
    return outputs


def _conversion_outputs_of_chunking_run(session: "Session", chunking_run_id: int) -> set[str]:
    """Return the conversion output ids a chunking run is current for: its own plus any applicability rows."""
    inp = run_input(session, chunking_run_id)
    outputs = {inp.conversion_output_id} if inp is not None else set()
    outputs.update(
        session.exec(
            select(ChunkRunApplicability.conversion_output_id).where(ChunkRunApplicability.run_id == chunking_run_id)
        ).all()
    )
    return outputs


def record_contextualization_run_applicability(
    session: "Session",
    *,
    run_id: int,
    summary_run_id: int,
    chunking_run_id: int,
    model_config: ModelConfig,
    splitter_version: int,
    context_version: int,
) -> ContextualizationRunApplicability:
    """Validate and stage a variant-run applicability relation; the caller commits.

    The relation is append-only and idempotent on
    ``(run_id, summary_run_id, chunking_run_id)``. Existing rows are returned
    unchanged.

    Raises:
        ValueError: If the variant run, upstream runs, fingerprints, counts, or
            shared conversion provenance is inconsistent.
    """
    run = require_active_run(session, run_id, VARIANT_STAGE, label="variant run")

    summary_run = session.get(PipelineRun, summary_run_id)
    if summary_run is None or summary_run.stage != SUMMARY_STAGE or summary_run.scope_id != run.scope_id:
        raise ValueError(f"summary run {summary_run_id!r} is missing or is not a summary run for {run.scope_id!r}")
    chunking_run = session.get(PipelineRun, chunking_run_id)
    if chunking_run is None or chunking_run.stage != CHUNKING_STAGE or chunking_run.scope_id != run.scope_id:
        raise ValueError(f"chunking run {chunking_run_id!r} is missing or is not a chunking run for {run.scope_id!r}")

    summary = session.exec(select(DocumentSummary).where(DocumentSummary.run_id == summary_run_id)).first()
    if summary is None:
        raise ValueError(f"summary run {summary_run_id!r} has no recorded DocumentSummary (a torn run)")
    chunk_keys = [chunk_key(chunk) for chunk, _, _ in document_order_chunks(session, chunking_run_id)]
    input_fingerprint = contextualization_run_input_fingerprint(summary.summary_text, chunk_keys)

    candidate_key = derivation_key(
        inputs={
            "input": input_fingerprint,
            "config": contextualization_config_fingerprint(splitter_version, context_version, model_config),
        }
    )
    if candidate_key != run.derivation_key:
        raise ValueError(f"variant run {run_id!r}'s derivation key does not match the recomputed candidate key")

    manifest_size = len(manifest_of_run(session, chunking_run_id))
    variant_count = len(session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == run_id)).all())
    if variant_count != manifest_size:
        raise ValueError(
            f"variant run {run_id!r} has {variant_count} variants, expected {manifest_size} (one per manifested chunk)"
        )

    summary_outputs = _conversion_outputs_of_summary_run(session, summary_run_id)
    chunking_outputs = _conversion_outputs_of_chunking_run(session, chunking_run_id)
    if summary_outputs.isdisjoint(chunking_outputs):
        raise ValueError(
            f"summary run {summary_run_id!r} and chunking run {chunking_run_id!r} share no conversion output"
        )

    return record_applicability(
        session,
        ContextualizationRunApplicability,
        {"run_id": run_id, "summary_run_id": summary_run_id, "chunking_run_id": chunking_run_id},
        input_fingerprint=input_fingerprint,
    )
