"""Conformance tests: chunk-contextualization under the pipeline-identity rules.

Demonstrates on the concrete contextualization stage the cross-cutting rules
defined once in `pipeline-identity` (lazy invalidation, the large-reprocessing
confirmation gate, and stochastic-producer surrogate identity), rather than
restating them as `chunk-contextualization` requirements:

- a ``context_version`` bump marks the active contextualized generation logically
  stale without eagerly recomputing it;
- a corpus-wide re-contextualization is gated behind explicit confirmation;
- a version-heterogeneous corpus is valid and each variant's producer version is
  queryable;
- contextualized-variant identity is a run-scoped row surrogate, and
  re-contextualization mints new variant identities under a superseding run.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlmodel import Session, select
import xxhash

from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk
from aizk.graph._version import CONTEXT_VERSION
from aizk.graph.contextualization import VARIANT_STAGE, contextualize_chunks, summarize_document
from aizk.graph.datamodel import ContextualizedChunk
from aizk.graph.llm import StubLLMClient
from aizk.graph.persistence import persist_chunks
from aizk.graph.workunit import enqueue_backfill
from aizk.pipeline.invalidation import ReprocessingConfirmationError, generation_is_stale
from aizk.pipeline.run import PipelineRun, RunStatus

_SOURCE_A = "11111111-1111-1111-1111-111111111111"
_SOURCE_B = "22222222-2222-2222-2222-222222222222"
_HASH_A = "0011223344556677"
_HASH_B = "aabbccddeeff0011"
_OUTPUT = "output-1"
_DOC_TEXT = "# Title\n\nThe document body the summary pass reads."


def _make_chunk(text: str, *, ordinal: int, source_id: str, markdown_hash: str) -> SplitterChunk:
    """Build a splitter chunk (no chunk_id — identity is assigned at persistence)."""
    content_hash = xxhash.xxh64(text.encode("utf-8")).hexdigest()
    return SplitterChunk(
        content_hash=content_hash,
        source_id=source_id,
        heading_path=(),
        ordinal=ordinal,
        text=text,
        char_count=len(text),
        converted_artifact_id=_OUTPUT,
        markdown_hash_xx64=markdown_hash,
        span=(0, len(text)),
        splitter_version=SPLITTER_VERSION,
    )


def _contextualize_source(
    session: Session,
    client: StubLLMClient,
    *,
    source_id: str,
    markdown_hash: str,
    context_version: int = CONTEXT_VERSION,
    texts: tuple[str, ...] = ("alpha", "beta"),
) -> list[ContextualizedChunk]:
    """Persist, summarize, and contextualize one source's chunks; return its variants."""
    chunks = [_make_chunk(t, ordinal=i, source_id=source_id, markdown_hash=markdown_hash) for i, t in enumerate(texts)]
    run, persisted = persist_chunks(
        session,
        source_id=source_id,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=markdown_hash,
        splitter_version=SPLITTER_VERSION,
        chunks=chunks,
    )
    summary = summarize_document(
        session,
        client,
        source_id=source_id,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=markdown_hash,
        document_text=_DOC_TEXT,
    )
    variants = contextualize_chunks(
        session,
        client,
        source_id=source_id,
        summary=summary,
        chunks=persisted,
        chunking_run_id=run.id,
        splitter_version=SPLITTER_VERSION,
        context_version=context_version,
    )
    session.commit()
    return variants


def _active_variant_run(session: Session, source_id: str) -> PipelineRun:
    """Return the active contextualization variant run for a source."""
    return session.exec(
        select(PipelineRun).where(
            PipelineRun.stage == VARIANT_STAGE,
            PipelineRun.scope_id == source_id,
            PipelineRun.status == RunStatus.ACTIVE,
        )
    ).one()


def test_context_version_bump_marks_stale_no_eager_recompute(session: Session) -> None:
    """Bumping ``context_version`` flags the active variant generation stale without recomputing it."""
    variants = _contextualize_source(session, StubLLMClient(), source_id=_SOURCE_A, markdown_hash=_HASH_A)
    variant_run = _active_variant_run(session, _SOURCE_A)

    # A bump is a read-only staleness flag — no recompute.
    assert generation_is_stale(variant_run, version_field="context_version", current_version=CONTEXT_VERSION + 1)
    assert not generation_is_stale(variant_run, version_field="context_version", current_version=CONTEXT_VERSION)

    # The stale generation remains the single active variant run, unmutated and usable.
    runs = session.exec(select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE)).all()
    assert len(runs) == 1 and runs[0].id == variant_run.id and runs[0].status is RunStatus.ACTIVE
    assert len(session.exec(select(ContextualizedChunk)).all()) == len(variants), "no variant was recomputed"


def test_corpus_recontextualization_requires_confirmation(session: Session) -> None:
    """A corpus-wide re-contextualization enqueue does not run until explicitly confirmed."""
    documents = [(101, UUID(_SOURCE_A)), (102, UUID(_SOURCE_B))]

    with pytest.raises(ReprocessingConfirmationError, match="will not run until it is explicitly confirmed"):
        enqueue_backfill(session, documents, confirmed=False)
    from aizk.graph.datamodel import ContextualizationJob

    assert session.exec(select(ContextualizationJob)).all() == [], "nothing is enqueued without confirmation"

    jobs = enqueue_backfill(session, documents, confirmed=True)
    session.commit()
    assert len(jobs) == 2, "explicit confirmation lets the corpus-wide backfill enqueue"


def test_mixed_version_corpus_valid_and_queryable(session: Session) -> None:
    """A corpus with variants on more than one ``context_version`` is valid and each version is queryable."""
    client = StubLLMClient()
    _contextualize_source(session, client, source_id=_SOURCE_A, markdown_hash=_HASH_A, context_version=CONTEXT_VERSION)
    _contextualize_source(
        session, client, source_id=_SOURCE_B, markdown_hash=_HASH_B, context_version=CONTEXT_VERSION + 1
    )

    all_variants = session.exec(select(ContextualizedChunk)).all()
    assert all_variants, "the mixed-version corpus holds variants"
    # Each variant records the producer version that produced it.
    assert {v.context_version for v in all_variants} == {CONTEXT_VERSION, CONTEXT_VERSION + 1}

    # Any version's coverage is queryable.
    v1 = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.context_version == CONTEXT_VERSION)).all()
    v2 = session.exec(
        select(ContextualizedChunk).where(ContextualizedChunk.context_version == CONTEXT_VERSION + 1)
    ).all()
    assert v1 and v2
    assert all(v.context_version == CONTEXT_VERSION for v in v1)
    assert all(v.context_version == CONTEXT_VERSION + 1 for v in v2)


def test_variant_identity_conforms(session: Session) -> None:
    """Variant identity is a run-scoped row surrogate; re-contextualization mints new identities."""
    first = _contextualize_source(session, StubLLMClient(), source_id=_SOURCE_A, markdown_hash=_HASH_A)
    first_run = _active_variant_run(session, _SOURCE_A)
    first_ids = {v.id for v in first}
    # The identity is an assigned row surrogate, not content-derived.
    assert all(isinstance(v.id, int) for v in first)
    assert len(first_ids) == len(first)

    # Re-contextualize under a bumped context_version: the variant run supersedes and
    # the stochastic producer mints new variant identities (no prior variant mutated).
    second = _contextualize_source(
        session, StubLLMClient(), source_id=_SOURCE_A, markdown_hash=_HASH_A, context_version=CONTEXT_VERSION + 1
    )
    second_run = _active_variant_run(session, _SOURCE_A)
    second_ids = {v.id for v in second}

    assert second_run.id != first_run.id
    assert session.get(PipelineRun, first_run.id).status is RunStatus.SUPERSEDED
    assert first_ids.isdisjoint(second_ids), "re-contextualization mints new variant identities"
    # The prior generation's variants remain present and unmodified under their run.
    prior = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == first_run.id)).all()
    assert {v.id for v in prior} == first_ids
