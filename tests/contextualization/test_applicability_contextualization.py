"""Validation-clause tests for the chunk, summary, and contextualization applicability writers.

Exercises :func:`~aizk.graph.chunks.record_chunk_run_applicability`,
:func:`~aizk.contextualization.core.applicability.record_summary_run_applicability`, and
:func:`~aizk.contextualization.core.applicability.record_contextualization_run_applicability`
against the six validation clauses shared design describes: the run exists and is
active for the relation's stage; every referenced input exists and belongs to the
run's scope; the recomputed candidate key (from the referenced inputs' persisted
fingerprints and the currently-resolved config) matches the run's derivation key;
the run's outputs are complete; contextualization only — the referenced summary
and chunking runs share at least one conversion output; and an identical relation
already present is idempotent, while one sharing a primary key with a different
``input_fingerprint`` is rejected.

The chunk and summary writers share clauses 1–4 and 6 (they have no counterpart to
clause 5, which is contextualization-only). A mismatched-config clause-3 rejection
for the chunk writer is covered in ``test_chunks.py`` alongside its
other write-time guards; it is not repeated here.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlmodel import Session, select
import xxhash

from aizk.ai.providers.llm import STUB_MODEL_CONFIG, StubLLMClient
from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk
from aizk.contextualization.core.applicability import (
    record_contextualization_run_applicability,
    record_summary_run_applicability,
)
from aizk.contextualization.core.keys import summary_input_fingerprint
from aizk.contextualization.core.summary import summarize_document
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.graph._version import CONTEXT_VERSION
from aizk.graph.chunks import (
    CHUNKING_STAGE,
    chunking_input_fingerprint,
    manifest_of_run,
    persist_chunks,
    record_chunk_run_applicability,
)
from aizk.graph.datamodel import (
    ChunkRunApplicability,
    ContextualizationRunApplicability,
    ContextualizedChunk,
    DocumentSummary,
    SummaryRunApplicability,
)
from aizk.graph.variants import SUMMARY_STAGE, VARIANT_STAGE
from aizk.pipeline.run import PipelineRun, RunStatus
from aizk.utilities.hashing import content_fingerprint

_AIZK_UUID = UUID("11111111-1111-1111-1111-111111111111")
_OTHER_UUID = UUID("22222222-2222-2222-2222-222222222222")
_OUTPUT_ID = "1001"
_OTHER_OUTPUT_ID = "1002"
_HASH_A = "0011223344556677"
_DOC_TEXT = "# Title\n\nThe document body the summary pass reads."


def _make_chunk(
    text: str, *, ordinal: int, source_id: UUID = _AIZK_UUID, markdown_hash: str = _HASH_A
) -> SplitterChunk:
    """Build a splitter chunk (no chunk_id — identity is assigned at persistence).

    ``span_start`` increases with ``ordinal`` so a document-order read
    (``span_start`` ascending, as :func:`~aizk.graph.chunks.document_order_chunks`
    reads it) agrees with the ordinal order the chunk was built and contextualized in.
    """
    content_hash = xxhash.xxh64(text.encode("utf-8")).hexdigest()
    span_start = ordinal * 1000
    return SplitterChunk(
        content_hash=content_hash,
        source_id=str(source_id),
        heading_path=(),
        ordinal=ordinal,
        text=text,
        char_count=len(text),
        converted_artifact_id=_OUTPUT_ID,
        markdown_hash_xx64=markdown_hash,
        span=(span_start, span_start + len(text)),
        splitter_version=SPLITTER_VERSION,
    )


def _seed_conversion_output(session: Session, *, output_id: str, source_id: UUID, markdown_hash: str) -> None:
    """Insert a minimal :class:`ConversionOutput` row for the writers' clause-2 lookup."""
    session.add(
        ConversionOutput(
            id=int(output_id),
            job_id=int(output_id),
            source_id=source_id,
            owner_id="test",
            title="doc",
            payload_version=1,
            s3_prefix="graph/test",
            markdown_key="markdown.md",
            manifest_key="manifest.json",
            markdown_hash_xx64=markdown_hash,
            docling_version="0",
            pipeline_name="test",
        )
    )
    session.commit()


def _persist_chunking(
    session: Session, chunks: list[SplitterChunk], *, output_id: str = _OUTPUT_ID
) -> tuple[PipelineRun, list[SplitterChunk]]:
    run, persisted = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=output_id,
        markdown_hash_xx64=_HASH_A,
        markdown_fingerprint=content_fingerprint(_HASH_A),
        splitter_version=SPLITTER_VERSION,
        chunks=chunks,
    )
    session.commit()
    return run, persisted


def _summarize(session: Session, *, output_id: str = _OUTPUT_ID, document_text: str = _DOC_TEXT) -> DocumentSummary:
    summary = summarize_document(
        session,
        StubLLMClient(),
        source_id=_AIZK_UUID,
        conversion_output_id=output_id,
        markdown_hash_xx64=_HASH_A,
        document_text=document_text,
        model_config=STUB_MODEL_CONFIG,
    )
    session.commit()
    return summary


# --------------------------------------------------------------------------- #
# Chunk-run applicability writer (clauses 1, 2, 4, 6; clause 3 lives in
# test_chunks.py alongside the writer's other call site)
# --------------------------------------------------------------------------- #


def test_chunk_applicability_rejects_a_missing_run(session: Session) -> None:
    """Clause 1: the run must exist."""
    with pytest.raises(ValueError, match="is missing, not active, or not a chunking run"):
        record_chunk_run_applicability(
            session,
            run_id=999_999,
            conversion_output_id=_OUTPUT_ID,
            input_fingerprint=chunking_input_fingerprint(content_fingerprint(_HASH_A)),
            splitter_version=SPLITTER_VERSION,
        )


def test_chunk_applicability_rejects_a_superseded_run(session: Session) -> None:
    """Clause 1: the run must be active, not superseded."""
    run_a, _ = _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    # A content change supersedes run_a.
    _persist_chunking(session, [_make_chunk("second", ordinal=0)])

    with pytest.raises(ValueError, match="is missing, not active, or not a chunking run"):
        record_chunk_run_applicability(
            session,
            run_id=run_a.id,
            conversion_output_id=_OUTPUT_ID,
            input_fingerprint=chunking_input_fingerprint(content_fingerprint(_HASH_A)),
            splitter_version=SPLITTER_VERSION,
        )


def test_chunk_applicability_rejects_a_conversion_output_from_another_source(session: Session) -> None:
    """Clause 2: the referenced conversion output must belong to the run's scope."""
    run, _ = _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    _seed_conversion_output(session, output_id=_OTHER_OUTPUT_ID, source_id=_OTHER_UUID, markdown_hash=_HASH_A)

    with pytest.raises(ValueError, match="does not exist or does not belong to source"):
        record_chunk_run_applicability(
            session,
            run_id=run.id,
            conversion_output_id=_OTHER_OUTPUT_ID,
            input_fingerprint=chunking_input_fingerprint(content_fingerprint(_HASH_A)),
            splitter_version=SPLITTER_VERSION,
        )


def test_chunk_applicability_rejects_a_torn_run_with_an_unexplained_empty_manifest(session: Session) -> None:
    """Clause 4: an empty manifest is only valid when the run genuinely recorded zero chunks."""
    run, _ = _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    _seed_conversion_output(session, output_id=_OTHER_OUTPUT_ID, source_id=_AIZK_UUID, markdown_hash=_HASH_A)
    # Simulate a torn write: the manifest is missing although the run's recorded
    # chunk_set_fingerprint says the run produced (non-empty) chunks.
    for entry in manifest_of_run(session, run.id):
        session.delete(entry)
    session.commit()

    with pytest.raises(ValueError, match="empty manifest but a non-empty recorded chunk set"):
        record_chunk_run_applicability(
            session,
            run_id=run.id,
            conversion_output_id=_OTHER_OUTPUT_ID,
            input_fingerprint=chunking_input_fingerprint(content_fingerprint(_HASH_A)),
            splitter_version=SPLITTER_VERSION,
        )


def test_chunk_applicability_is_idempotent(session: Session) -> None:
    """Clause 6: an identical relation already present is a no-op that returns the existing row."""
    run, _ = _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    _seed_conversion_output(session, output_id=_OTHER_OUTPUT_ID, source_id=_AIZK_UUID, markdown_hash=_HASH_A)
    fingerprint = chunking_input_fingerprint(content_fingerprint(_HASH_A))

    first = record_chunk_run_applicability(
        session,
        run_id=run.id,
        conversion_output_id=_OTHER_OUTPUT_ID,
        input_fingerprint=fingerprint,
        splitter_version=SPLITTER_VERSION,
    )
    session.commit()
    again = record_chunk_run_applicability(
        session,
        run_id=run.id,
        conversion_output_id=_OTHER_OUTPUT_ID,
        input_fingerprint=fingerprint,
        splitter_version=SPLITTER_VERSION,
    )
    assert again is first
    assert len(session.exec(select(ChunkRunApplicability).where(ChunkRunApplicability.run_id == run.id)).all()) == 1


def test_chunk_applicability_rejects_a_mismatched_duplicate(session: Session) -> None:
    """Clause 6: a relation sharing a primary key with a different ``input_fingerprint`` is rejected.

    Simulates a row written outside the writer (append-only, no update path) that
    disagrees with what the writer itself would compute and validate.
    """
    run, _ = _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    _seed_conversion_output(session, output_id=_OTHER_OUTPUT_ID, source_id=_AIZK_UUID, markdown_hash=_HASH_A)
    session.add(
        ChunkRunApplicability(run_id=run.id, conversion_output_id=_OTHER_OUTPUT_ID, input_fingerprint="stale-value")
    )
    session.commit()

    with pytest.raises(ValueError, match="different input_fingerprint"):
        record_chunk_run_applicability(
            session,
            run_id=run.id,
            conversion_output_id=_OTHER_OUTPUT_ID,
            input_fingerprint=chunking_input_fingerprint(content_fingerprint(_HASH_A)),
            splitter_version=SPLITTER_VERSION,
        )


# --------------------------------------------------------------------------- #
# Summary-run applicability writer (clauses 1, 2, 3, 4, 6)
# --------------------------------------------------------------------------- #


def test_summary_applicability_rejects_a_missing_run(session: Session) -> None:
    """Clause 1: the run must exist."""
    with pytest.raises(ValueError, match="is missing, not active, or not a summary run"):
        record_summary_run_applicability(
            session,
            run_id=999_999,
            conversion_output_id=_OUTPUT_ID,
            input_fingerprint=summary_input_fingerprint(_DOC_TEXT),
            model_config=STUB_MODEL_CONFIG,
            summary_version=1,
        )


def test_summary_applicability_rejects_a_superseded_run(session: Session) -> None:
    """Clause 1: the run must be active, not superseded."""
    _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    first_summary = _summarize(session)
    # A changed document supersedes the first summary run.
    _summarize(session, document_text=_DOC_TEXT + " changed")

    with pytest.raises(ValueError, match="is missing, not active, or not a summary run"):
        record_summary_run_applicability(
            session,
            run_id=first_summary.run_id,
            conversion_output_id=_OUTPUT_ID,
            input_fingerprint=summary_input_fingerprint(_DOC_TEXT),
            model_config=STUB_MODEL_CONFIG,
            summary_version=1,
        )


def test_summary_applicability_rejects_a_conversion_output_from_another_source(session: Session) -> None:
    """Clause 2: the referenced conversion output must belong to the run's scope."""
    _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    summary = _summarize(session)
    _seed_conversion_output(session, output_id=_OTHER_OUTPUT_ID, source_id=_OTHER_UUID, markdown_hash=_HASH_A)

    with pytest.raises(ValueError, match="does not exist or does not belong to source"):
        record_summary_run_applicability(
            session,
            run_id=summary.run_id,
            conversion_output_id=_OTHER_OUTPUT_ID,
            input_fingerprint=summary_input_fingerprint(_DOC_TEXT),
            model_config=STUB_MODEL_CONFIG,
            summary_version=summary.summary_version,
        )


def test_summary_applicability_rejects_a_mismatched_candidate_key(session: Session) -> None:
    """Clause 3: the recomputed candidate key must equal the run's derivation key."""
    _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    summary = _summarize(session)
    _seed_conversion_output(session, output_id=_OTHER_OUTPUT_ID, source_id=_AIZK_UUID, markdown_hash=_HASH_A)

    with pytest.raises(ValueError, match="derivation key does not match"):
        record_summary_run_applicability(
            session,
            run_id=summary.run_id,
            conversion_output_id=_OTHER_OUTPUT_ID,
            # A document-text fingerprint the run was never keyed on.
            input_fingerprint=summary_input_fingerprint(_DOC_TEXT + " never summarized"),
            model_config=STUB_MODEL_CONFIG,
            summary_version=summary.summary_version,
        )


def test_summary_applicability_rejects_a_torn_run_with_no_document_summary(session: Session) -> None:
    """Clause 4: the run's ``DocumentSummary`` must exist."""
    _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    summary = _summarize(session)
    _seed_conversion_output(session, output_id=_OTHER_OUTPUT_ID, source_id=_AIZK_UUID, markdown_hash=_HASH_A)
    # Simulate a torn write: the run exists and is active, but its DocumentSummary is gone.
    session.delete(session.get(DocumentSummary, summary.id))
    session.commit()

    with pytest.raises(ValueError, match="has no recorded DocumentSummary"):
        record_summary_run_applicability(
            session,
            run_id=summary.run_id,
            conversion_output_id=_OTHER_OUTPUT_ID,
            input_fingerprint=summary_input_fingerprint(_DOC_TEXT),
            model_config=STUB_MODEL_CONFIG,
            summary_version=summary.summary_version,
        )


def test_summary_applicability_is_idempotent(session: Session) -> None:
    """Clause 6: an identical relation already present is a no-op that returns the existing row."""
    _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    summary = _summarize(session)
    _seed_conversion_output(session, output_id=_OTHER_OUTPUT_ID, source_id=_AIZK_UUID, markdown_hash=_HASH_A)
    fingerprint = summary_input_fingerprint(_DOC_TEXT)

    first = record_summary_run_applicability(
        session,
        run_id=summary.run_id,
        conversion_output_id=_OTHER_OUTPUT_ID,
        input_fingerprint=fingerprint,
        model_config=STUB_MODEL_CONFIG,
        summary_version=summary.summary_version,
    )
    session.commit()
    again = record_summary_run_applicability(
        session,
        run_id=summary.run_id,
        conversion_output_id=_OTHER_OUTPUT_ID,
        input_fingerprint=fingerprint,
        model_config=STUB_MODEL_CONFIG,
        summary_version=summary.summary_version,
    )
    assert again is first
    rows = session.exec(select(SummaryRunApplicability).where(SummaryRunApplicability.run_id == summary.run_id)).all()
    assert len(rows) == 1


def test_summary_applicability_rejects_a_mismatched_duplicate(session: Session) -> None:
    """Clause 6: a relation sharing a primary key with a different ``input_fingerprint`` is rejected.

    Simulates a row written outside the writer (append-only, no update path) that
    disagrees with what the writer itself would compute and validate.
    """
    _persist_chunking(session, [_make_chunk("first", ordinal=0)])
    summary = _summarize(session)
    _seed_conversion_output(session, output_id=_OTHER_OUTPUT_ID, source_id=_AIZK_UUID, markdown_hash=_HASH_A)
    session.add(
        SummaryRunApplicability(
            run_id=summary.run_id, conversion_output_id=_OTHER_OUTPUT_ID, input_fingerprint="stale-value"
        )
    )
    session.commit()

    with pytest.raises(ValueError, match="different input_fingerprint"):
        record_summary_run_applicability(
            session,
            run_id=summary.run_id,
            conversion_output_id=_OTHER_OUTPUT_ID,
            input_fingerprint=summary_input_fingerprint(_DOC_TEXT),
            model_config=STUB_MODEL_CONFIG,
            summary_version=summary.summary_version,
        )


# --------------------------------------------------------------------------- #
# Contextualization-run applicability writer (all six clauses)
# --------------------------------------------------------------------------- #


def _seed_contextualization(
    session: Session, *, output_id: str = _OUTPUT_ID
) -> tuple[PipelineRun, DocumentSummary, list]:
    """Persist chunks, summarize, and contextualize under a single conversion output; return (chunking, summary, variants)."""
    from aizk.contextualization.core.revisions import contextualize_chunks

    chunking_run, chunks = _persist_chunking(
        session, [_make_chunk("alpha", ordinal=0), _make_chunk("beta", ordinal=1)], output_id=output_id
    )
    summary = _summarize(session, output_id=output_id)
    variants = contextualize_chunks(
        session,
        StubLLMClient(),
        source_id=_AIZK_UUID,
        summary=summary,
        chunks=chunks,
        chunking_run_id=chunking_run.id,
        splitter_version=SPLITTER_VERSION,
        model_config=STUB_MODEL_CONFIG,
    )
    session.commit()
    return chunking_run, summary, variants


def test_contextualization_applicability_rejects_a_missing_run(session: Session) -> None:
    """Clause 1: the run must exist."""
    chunking_run, summary, _ = _seed_contextualization(session)

    with pytest.raises(ValueError, match="is missing, not active, or not a variant run"):
        record_contextualization_run_applicability(
            session,
            run_id=999_999,
            summary_run_id=summary.run_id,
            chunking_run_id=chunking_run.id,
            model_config=STUB_MODEL_CONFIG,
            splitter_version=SPLITTER_VERSION,
            context_version=CONTEXT_VERSION,
        )


def test_contextualization_applicability_rejects_a_summary_run_for_another_source(session: Session) -> None:
    """Clause 2: the referenced summary run must be a summary run scoped to the variant run's source."""
    chunking_run, _summary, variants = _seed_contextualization(session)
    active_variant_run = session.exec(
        select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()
    foreign_summary_run = PipelineRun(
        stage=SUMMARY_STAGE,
        scope_id=_OTHER_UUID,
        status=RunStatus.ACTIVE,
        derivation_key="foreign",
        version_stamps_json="{}",
    )
    session.add(foreign_summary_run)
    session.flush()

    with pytest.raises(ValueError, match="is missing or is not a summary run"):
        record_contextualization_run_applicability(
            session,
            run_id=active_variant_run.id,
            summary_run_id=foreign_summary_run.id,
            chunking_run_id=chunking_run.id,
            model_config=STUB_MODEL_CONFIG,
            splitter_version=SPLITTER_VERSION,
            context_version=CONTEXT_VERSION,
        )


def test_contextualization_applicability_rejects_a_chunking_run_for_another_source(session: Session) -> None:
    """Clause 2: the referenced chunking run must be a chunking run scoped to the variant run's source."""
    _chunking_run, summary, _variants = _seed_contextualization(session)
    active_variant_run = session.exec(
        select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()
    foreign_chunking_run = PipelineRun(
        stage=CHUNKING_STAGE,
        scope_id=_OTHER_UUID,
        status=RunStatus.ACTIVE,
        derivation_key="foreign",
        version_stamps_json="{}",
    )
    session.add(foreign_chunking_run)
    session.flush()

    with pytest.raises(ValueError, match="is missing or is not a chunking run"):
        record_contextualization_run_applicability(
            session,
            run_id=active_variant_run.id,
            summary_run_id=summary.run_id,
            chunking_run_id=foreign_chunking_run.id,
            model_config=STUB_MODEL_CONFIG,
            splitter_version=SPLITTER_VERSION,
            context_version=CONTEXT_VERSION,
        )


def test_contextualization_applicability_rejects_a_mismatched_candidate_key(session: Session) -> None:
    """Clause 3: the recomputed candidate key must equal the run's derivation key."""
    chunking_run, summary, _variants = _seed_contextualization(session)
    active_variant_run = session.exec(
        select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()

    with pytest.raises(ValueError, match="derivation key does not match"):
        record_contextualization_run_applicability(
            session,
            run_id=active_variant_run.id,
            summary_run_id=summary.run_id,
            chunking_run_id=chunking_run.id,
            # A resolved config the run was never keyed on.
            model_config=STUB_MODEL_CONFIG,
            splitter_version=SPLITTER_VERSION + 1,
            context_version=CONTEXT_VERSION,
        )


def test_contextualization_applicability_rejects_a_torn_run_missing_variants(session: Session) -> None:
    """Clause 4: the variant count must equal the referenced chunking run's manifest size."""
    chunking_run, summary, variants = _seed_contextualization(session)
    active_variant_run = session.exec(
        select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()
    # Simulate a torn write: one variant is missing relative to the manifest,
    # while the (summary, chunking) pair and hence the candidate key are unchanged.
    session.delete(session.get(ContextualizedChunk, variants[0].id))
    session.commit()

    with pytest.raises(ValueError, match="expected .* \\(one per manifested chunk\\)"):
        record_contextualization_run_applicability(
            session,
            run_id=active_variant_run.id,
            summary_run_id=summary.run_id,
            chunking_run_id=chunking_run.id,
            model_config=STUB_MODEL_CONFIG,
            splitter_version=SPLITTER_VERSION,
            context_version=CONTEXT_VERSION,
        )


def test_contextualization_applicability_rejects_summary_and_chunking_sharing_no_output(session: Session) -> None:
    """Clause 5: the referenced summary and chunking runs must share at least one conversion output."""
    chunking_run, summary, _variants = _seed_contextualization(session, output_id=_OUTPUT_ID)
    active_variant_run = session.exec(
        select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()
    # A second summary run with byte-identical text (a version bump) satisfies
    # clause 3's candidate key, but it consumed a distinct conversion output the
    # chunking run was never current for — so clause 5 must reject it.
    other_summary = summarize_document(
        session,
        StubLLMClient(responder=lambda p: summary.summary_text if "summary_prompt" in p else "rev"),
        source_id=_AIZK_UUID,
        conversion_output_id=_OTHER_OUTPUT_ID,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
        model_config=STUB_MODEL_CONFIG,
        summary_version=summary.summary_version + 1,
    )
    session.commit()
    assert other_summary.summary_text == summary.summary_text

    with pytest.raises(ValueError, match="share no conversion output"):
        record_contextualization_run_applicability(
            session,
            run_id=active_variant_run.id,
            summary_run_id=other_summary.run_id,
            chunking_run_id=chunking_run.id,
            model_config=STUB_MODEL_CONFIG,
            splitter_version=SPLITTER_VERSION,
            context_version=CONTEXT_VERSION,
        )


def test_contextualization_applicability_is_idempotent(session: Session) -> None:
    """Clause 6: an identical relation already present is a no-op that returns the existing row.

    The writer computes ``input_fingerprint`` itself from the referenced summary
    and chunking runs' immutable persisted state, so a mismatched duplicate for the
    same primary key cannot arise through the writer's public entry point (unlike
    the chunk and summary writers, which accept a caller-supplied fingerprint); the
    reachable behavior for clause 6 here is idempotent reuse.
    """
    chunking_run, summary, _variants = _seed_contextualization(session)
    active_variant_run = session.exec(
        select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()
    # A second summary run with byte-identical text (a version bump) is a genuinely
    # different (summary_run_id, chunking_run_id) pair whose candidate key matches.
    second_summary = summarize_document(
        session,
        StubLLMClient(responder=lambda p: summary.summary_text if "summary_prompt" in p else "rev"),
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT_ID,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
        model_config=STUB_MODEL_CONFIG,
        summary_version=summary.summary_version + 1,
    )
    session.commit()
    assert second_summary.summary_text == summary.summary_text

    first = record_contextualization_run_applicability(
        session,
        run_id=active_variant_run.id,
        summary_run_id=second_summary.run_id,
        chunking_run_id=chunking_run.id,
        model_config=STUB_MODEL_CONFIG,
        splitter_version=SPLITTER_VERSION,
        context_version=CONTEXT_VERSION,
    )
    session.commit()
    again = record_contextualization_run_applicability(
        session,
        run_id=active_variant_run.id,
        summary_run_id=second_summary.run_id,
        chunking_run_id=chunking_run.id,
        model_config=STUB_MODEL_CONFIG,
        splitter_version=SPLITTER_VERSION,
        context_version=CONTEXT_VERSION,
    )
    assert again is first
    rows = session.exec(
        select(ContextualizationRunApplicability).where(
            ContextualizationRunApplicability.run_id == active_variant_run.id
        )
    ).all()
    assert len(rows) == 1
