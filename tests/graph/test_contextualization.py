"""Behavioral tests for the summary and variant contextualization runs.

Exercises the ``chunk-contextualization`` revision through
:mod:`aizk.graph.contextualization` with a stubbed model: both runs are scoped to
the durable source identity (``source_id``); the summary run records its
derivation key, version, and consumed ``conversion_output_id`` and supersedes on
input change; the variant run records per-chunk ``summary_run_id`` /
``chunking_run_id`` provenance and folds ``splitter_version`` into its derivation
key, superseding on neighbor/summary/splitter/version change; the source chunk is
never modified; and every model call passes through the one injected access
point. The variant stores the model's self-contained revision of the chunk (empty
when already self-contained). Output *text* is non-deterministic in production, so
these assert structure and provenance, never exact model output.

Variant derivation keys embed each chunk's portable content key
(``derive_chunk_content_key``), not its database-local surrogate ``chunk_id``, so
the keys recompute identically on any backend; the surrogate is recorded only as
provenance (the variant's ``chunk_id`` and the manifest edge).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk
from aizk.chunking.datamodel import derive_chunk_content_key
from aizk.graph._version import CONTEXT_VERSION, SUMMARY_VERSION
from aizk.graph.content_index import CONTENT_FTS_DDL
import aizk.graph.contextualization as ctx_mod
from aizk.graph.contextualization import (
    SUMMARY_STAGE,
    VARIANT_STAGE,
    ContextSource,
    contextualize_chunks,
    resolve_chunk_text,
    summarize_document,
)
from aizk.graph.datamodel import Chunk, ContextualizedChunk, DocumentSummary
from aizk.graph.llm import StubLLMClient
from aizk.graph.persistence import manifest_of_run, persist_chunks, run_input
from aizk.pipeline.run import PipelineRun, RunStatus

_AIZK_UUID = "11111111-1111-1111-1111-111111111111"
_OUTPUT = "output-1"
_OUTPUT_B = "output-2"
_HASH_A = "0011223344556677"
_HASH_B = "aabbccddeeff0011"
_DOC_TEXT = "# Title\n\nThe document body the summary pass reads."


def _make_chunk(
    text: str,
    *,
    ordinal: int,
    markdown_hash: str = _HASH_A,
    conversion_output_id: str = _OUTPUT,
    source_id: str = _AIZK_UUID,
    splitter_version: int = SPLITTER_VERSION,
) -> SplitterChunk:
    """Build a splitter chunk for a single source (no chunk_id — identity is assigned at persistence)."""
    content_hash = xxhash.xxh64(text.encode("utf-8")).hexdigest()
    return SplitterChunk(
        content_hash=content_hash,
        source_id=source_id,
        heading_path=(),
        ordinal=ordinal,
        text=text,
        char_count=len(text),
        converted_artifact_id=conversion_output_id,
        markdown_hash_xx64=markdown_hash,
        span=(0, len(text)),
        splitter_version=splitter_version,
    )


def _content_key(chunk: SplitterChunk) -> str:
    """The chunk's portable content key — what variant derivation keys embed (not the surrogate)."""
    return derive_chunk_content_key(chunk.source_id, chunk.heading_path, chunk.ordinal, chunk.content_hash)


def _persist(
    session: Session,
    chunks: list[SplitterChunk],
    *,
    markdown_hash: str,
    conversion_output_id: str = _OUTPUT,
    splitter_version: int = SPLITTER_VERSION,
) -> tuple[PipelineRun, list[SplitterChunk]]:
    """Persist a chunk set; return its chunking run and the chunks carrying their assigned surrogate ``chunk_id``."""
    run, persisted = persist_chunks(
        session,
        source_id=_AIZK_UUID,
        conversion_output_id=conversion_output_id,
        markdown_hash_xx64=markdown_hash,
        splitter_version=splitter_version,
        chunks=chunks,
    )
    session.commit()
    return run, persisted


def _runs(session: Session, stage: str) -> list[PipelineRun]:
    return list(session.exec(select(PipelineRun).where(PipelineRun.stage == stage)).all())


def _summarize_and_contextualize(
    session: Session,
    client: StubLLMClient,
    chunks: list[SplitterChunk],
    *,
    markdown_hash: str,
    chunking_run_id: int,
    conversion_output_id: str = _OUTPUT,
    splitter_version: int = SPLITTER_VERSION,
) -> list[ContextualizedChunk]:
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=conversion_output_id,
        markdown_hash_xx64=markdown_hash,
        document_text=_DOC_TEXT,
    )
    variants = contextualize_chunks(
        session,
        client,
        source_id=_AIZK_UUID,
        summary=summary,
        chunks=chunks,
        chunking_run_id=chunking_run_id,
        splitter_version=splitter_version,
    )
    session.commit()
    return variants


# --------------------------------------------------------------------------- #
# Summary run — unchanged / changed-input partition
# --------------------------------------------------------------------------- #


def test_summary_with_derivation_key_and_version(session: Session) -> None:
    """A processed document yields one summary recording markdown hash, version, and consumed output."""
    _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient()

    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    session.commit()

    summaries = session.exec(select(DocumentSummary)).all()
    assert len(summaries) == 1
    assert summary.markdown_hash_xx64 == _HASH_A
    assert summary.summary_version == SUMMARY_VERSION
    assert summary.conversion_output_id == _OUTPUT, "the summary records the Markdown locator it consumed"
    active = session.exec(
        select(PipelineRun).where(PipelineRun.stage == SUMMARY_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()
    assert summary.run_id == active.id
    assert active.scope_id == _AIZK_UUID, "the summary run is scoped to the durable source identity"
    derivation_key = json.loads(active.derivation_key)
    version_stamps = json.loads(active.version_stamps_json)
    assert derivation_key["summary_prompt_hash"] == getattr(ctx_mod, "SUMMARY_PROMPT_HASH", None)
    assert version_stamps["summary_prompt_hash"] == getattr(ctx_mod, "SUMMARY_PROMPT_HASH", None)


def test_unchanged_inputs_no_new_run(session: Session) -> None:
    """Re-summarizing with unchanged markdown and version makes no new run or summary."""
    _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient()

    first = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    session.commit()
    second = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    session.commit()

    assert second.id == first.id
    assert len(_runs(session, SUMMARY_STAGE)) == 1
    assert len(session.exec(select(DocumentSummary)).all()) == 1
    assert len(client.prompts) == 1, "the unchanged re-run makes no model call"


def test_changed_markdown_supersedes(session: Session) -> None:
    """Changed source markdown opens a new summary run; the prior summary is retained."""
    _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient()
    first = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    session.commit()
    first_text = first.summary_text

    second = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT_B,
        markdown_hash_xx64=_HASH_B,
        document_text="# New\n\nchanged",
    )
    session.commit()

    assert second.id != first.id
    runs = {r.status for r in _runs(session, SUMMARY_STAGE)}
    assert runs == {RunStatus.ACTIVE, RunStatus.SUPERSEDED}
    # Prior summary row remains present and unmodified.
    prior = session.get(DocumentSummary, first.id)
    assert prior is not None
    assert prior.markdown_hash_xx64 == _HASH_A
    assert prior.summary_text == first_text


# --------------------------------------------------------------------------- #
# Variant run — unchanged / changed-input partition
# --------------------------------------------------------------------------- #


def test_variant_with_provenance_and_derivation_key(session: Session) -> None:
    """Each chunk gets a separately addressable variant carrying full 2p/1n provenance."""
    chunking_run, chunks = _persist(
        session,
        [
            _make_chunk("first", ordinal=0),
            _make_chunk("second", ordinal=1),
            _make_chunk("middle", ordinal=2),
            _make_chunk("last", ordinal=3),
        ],
        markdown_hash=_HASH_A,
    )
    client = StubLLMClient()

    variants = _summarize_and_contextualize(
        session, client, chunks, markdown_hash=_HASH_A, chunking_run_id=chunking_run.id
    )

    assert len(variants) == len(chunks)
    by_chunk = {v.chunk_id: v for v in variants}
    assert set(by_chunk) == {c.chunk_id for c in chunks}
    summary = session.exec(select(DocumentSummary)).one()
    summary_run = session.get(PipelineRun, summary.run_id)
    assert summary_run is not None

    for variant in variants:
        assert variant.id is not None, "variant is separately addressable"
        assert variant.context_version == CONTEXT_VERSION
        assert variant.chunking_run_id == chunking_run.id, "variant points at the chunking generation it read"
        assert variant.summary_run_id == summary.run_id
        row_key = json.loads(variant.derivation_key)
        summary_identity = json.loads(row_key["summary_identity"])
        assert summary_identity["summary_derivation_key"] == summary_run.derivation_key
        assert row_key["splitter_version"] == SPLITTER_VERSION
        assert "summary_id" not in row_key
        # The surrogate chunk_id is never a derivation-key input.
        assert all(field not in row_key for field in ("working_chunk_id", "prior_chunk_1_id", "next_chunk_1_id"))
        assert row_key["model_profile"] == getattr(ctx_mod, "DEFAULT_MODEL_PROFILE", None)

    active = session.exec(
        select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()
    assert active.scope_id == _AIZK_UUID, "the variant run is scoped to the durable source identity"
    run_key = json.loads(active.derivation_key)
    version_stamps = json.loads(active.version_stamps_json)
    assert run_key["context_prompt_hash"] == getattr(ctx_mod, "CONTEXT_PROMPT_HASH", None)
    assert run_key["context_window_policy"] == getattr(ctx_mod, "CONTEXT_WINDOW_POLICY", None)
    assert run_key["splitter_version"] == SPLITTER_VERSION
    # The run-level chunk set is identified by content keys, not surrogate chunk_ids.
    assert run_key["chunk_keys"] == [_content_key(c) for c in chunks]
    assert "chunk_ids" not in run_key
    assert "summary_id" not in run_key
    assert "chunking_run_id" not in run_key, "the chunking-run locator stays out of the derivation key"
    assert "summary_run_id" not in version_stamps
    assert version_stamps["summary_derivation_key"] == summary_run.derivation_key
    assert version_stamps["splitter_version"] == str(SPLITTER_VERSION)
    assert version_stamps["context_prompt_hash"] == getattr(ctx_mod, "CONTEXT_PROMPT_HASH", None)
    assert version_stamps["context_window_policy"] == getattr(ctx_mod, "CONTEXT_WINDOW_POLICY", None)

    middle = by_chunk[chunks[2].chunk_id]
    middle_key = json.loads(middle.derivation_key)
    assert middle_key["model_profile"] == getattr(ctx_mod, "DEFAULT_MODEL_PROFILE", None)
    # The 2p/1n window is recorded by content key.
    assert middle_key["working_chunk_key"] == _content_key(chunks[2])
    assert middle_key["prior_chunk_2_key"] == _content_key(chunks[0])
    assert middle_key["prior_chunk_1_key"] == _content_key(chunks[1])
    assert middle_key["next_chunk_1_key"] == _content_key(chunks[3])


def test_variant_derivation_key_ignores_local_summary_ids(tmp_path: Path) -> None:
    """Variant derivation is stable across databases with different summary row ids."""

    def process_document(
        db_name: str, *, dummy_summary_count: int
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        engine = create_engine(f"sqlite:///{tmp_path / db_name}")
        SQLModel.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text(CONTENT_FTS_DDL))
        client = StubLLMClient(
            responder=lambda prompt: "stable summary" if "summary_prompt" in prompt else "stable revision"
        )

        with Session(engine) as local_session:
            for index in range(dummy_summary_count):
                local_session.add(
                    DocumentSummary(
                        run_id=10_000 + index,
                        conversion_output_id=f"dummy-{index}",
                        summary_text="dummy",
                        markdown_hash_xx64="deadbeefdeadbeef",
                        summary_version=SUMMARY_VERSION,
                    )
                )
            local_session.commit()

            chunking_run, chunks = _persist(
                local_session,
                [_make_chunk("first", ordinal=0), _make_chunk("second", ordinal=1)],
                markdown_hash=_HASH_A,
            )
            _summarize_and_contextualize(
                local_session, client, chunks, markdown_hash=_HASH_A, chunking_run_id=chunking_run.id
            )
            active_run = local_session.exec(
                select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
            ).one()
            variant_keys = [
                json.loads(variant.derivation_key)
                for variant in local_session.exec(
                    select(ContextualizedChunk)
                    .where(ContextualizedChunk.run_id == active_run.id)
                    .order_by(ContextualizedChunk.chunk_id)
                ).all()
            ]
            return json.loads(active_run.derivation_key), variant_keys

    run_key_a, row_keys_a = process_document("a.db", dummy_summary_count=0)
    run_key_b, row_keys_b = process_document("b.db", dummy_summary_count=3)

    assert run_key_a == run_key_b
    assert "summary_id" not in run_key_a
    assert row_keys_a == row_keys_b
    assert all("summary_id" not in key for key in row_keys_a)


def test_variant_derivation_key_no_db_local_input(tmp_path: Path) -> None:
    """Variant run/row derivation keys are invariant when surrogate ``chunk_id``s differ but content keys match.

    The portability proxy for the stochastic stage (mirrors
    ``test_chunk_id_no_db_local_input``): two independent databases mint different
    surrogate ``chunk_id``s for the same logical chunk set, yet the variant run and
    per-row derivation keys are byte-identical because they embed each chunk's
    content key, never its database-local surrogate.
    """

    def process_document(db_name: str) -> tuple[list[str], dict[str, object], list[dict[str, object]]]:
        engine = create_engine(f"sqlite:///{tmp_path / db_name}")
        SQLModel.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text(CONTENT_FTS_DDL))
        client = StubLLMClient(
            responder=lambda prompt: "stable summary" if "summary_prompt" in prompt else "stable revision"
        )
        with Session(engine) as local_session:
            chunking_run, chunks = _persist(
                local_session,
                [_make_chunk("alpha", ordinal=0), _make_chunk("beta", ordinal=1)],
                markdown_hash=_HASH_A,
            )
            _summarize_and_contextualize(
                local_session, client, chunks, markdown_hash=_HASH_A, chunking_run_id=chunking_run.id
            )
            active_run = local_session.exec(
                select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
            ).one()
            variants = local_session.exec(
                select(ContextualizedChunk)
                .where(ContextualizedChunk.run_id == active_run.id)
                .order_by(ContextualizedChunk.derivation_key)
            ).all()
            surrogate_ids = sorted(c.chunk_id for c in chunks)
            return (
                surrogate_ids,
                json.loads(active_run.derivation_key),
                [json.loads(v.derivation_key) for v in variants],
            )

    ids_a, run_key_a, row_keys_a = process_document("a.db")
    ids_b, run_key_b, row_keys_b = process_document("b.db")

    # The surrogate identities genuinely differ between the two databases…
    assert ids_a != ids_b, "each database minted its own surrogate chunk_ids"
    assert all(sid not in ids_b for sid in ids_a)
    # …yet the derivation keys — keyed on content, not surrogate — are identical.
    assert run_key_a == run_key_b
    assert row_keys_a == row_keys_b
    # And no surrogate chunk_id leaks into any key.
    assert not any(sid in json.dumps(run_key_a) for sid in ids_a + ids_b)
    assert not any(sid in json.dumps(row_keys_a) for sid in ids_a + ids_b)


def test_zero_chunk_document_does_not_resupersede_on_reprocess(session: Session) -> None:
    """A document with no chunks reuses its empty variant run instead of churning new runs.

    The idempotency guard keys on the derivation key, so a legitimately empty
    variant run (zero-chunk document) is reused rather than superseded on every
    pass; reprocessing makes no new run and no extra model call.
    """
    chunking_run, _ = _persist(session, [], markdown_hash=_HASH_A)
    client = StubLLMClient()
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    contextualize_chunks(
        session,
        client,
        source_id=_AIZK_UUID,
        summary=summary,
        chunks=[],
        chunking_run_id=chunking_run.id,
        splitter_version=SPLITTER_VERSION,
    )
    session.commit()
    calls_after_first = len(client.prompts)

    contextualize_chunks(
        session,
        client,
        source_id=_AIZK_UUID,
        summary=summary,
        chunks=[],
        chunking_run_id=chunking_run.id,
        splitter_version=SPLITTER_VERSION,
    )
    session.commit()

    variant_runs = _runs(session, VARIANT_STAGE)
    assert len(variant_runs) == 1
    assert variant_runs[0].status == RunStatus.ACTIVE
    assert session.exec(select(ContextualizedChunk)).all() == []
    assert len(client.prompts) == calls_after_first, "reprocessing an empty document makes no model call"


def test_changed_neighbor_supersedes_variant(session: Session) -> None:
    """A changed neighbor opens a new variant run; the prior variant is retained."""
    run_a, (a, b) = _persist(
        session, [_make_chunk("alpha", ordinal=0), _make_chunk("beta", ordinal=1)], markdown_hash=_HASH_A
    )
    client = StubLLMClient()
    first_variants = _summarize_and_contextualize(
        session, client, [a, b], markdown_hash=_HASH_A, chunking_run_id=run_a.id
    )
    first_run_id = first_variants[0].run_id
    a_prior_text = next(v for v in first_variants if v.chunk_id == a.chunk_id).contextualized_text

    # Re-chunk: b is replaced by c, so a's next neighbor changes. The unchanged
    # section is re-emitted from the new markdown, so a carries _HASH_B now (same
    # content ⇒ the same sameness-key, hence the same reused surrogate chunk_id).
    run_b, (a_rechunked, c) = _persist(
        session,
        [
            _make_chunk("alpha", ordinal=0, markdown_hash=_HASH_B, conversion_output_id=_OUTPUT_B),
            _make_chunk("gamma", ordinal=1, markdown_hash=_HASH_B, conversion_output_id=_OUTPUT_B),
        ],
        markdown_hash=_HASH_B,
        conversion_output_id=_OUTPUT_B,
    )
    assert a_rechunked.chunk_id == a.chunk_id, "the unchanged section reuses its surrogate identity"
    _summarize_and_contextualize(
        session,
        client,
        [a_rechunked, c],
        markdown_hash=_HASH_B,
        chunking_run_id=run_b.id,
        conversion_output_id=_OUTPUT_B,
    )

    variant_runs = {r.id: r.status for r in _runs(session, VARIANT_STAGE)}
    assert variant_runs[first_run_id] == RunStatus.SUPERSEDED
    assert RunStatus.ACTIVE in variant_runs.values()
    # The prior variant for a remains present and unmodified.
    prior_a = session.exec(
        select(ContextualizedChunk).where(
            ContextualizedChunk.run_id == first_run_id, ContextualizedChunk.chunk_id == a.chunk_id
        )
    ).one()
    assert prior_a.contextualized_text == a_prior_text


def test_changed_splitter_version_supersedes_variant(session: Session) -> None:
    """A splitter_version bump opens a new variant run under the same context_version; prior retained."""
    run_v1, (chunk_v1,) = _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient()
    first_variants = _summarize_and_contextualize(
        session, client, [chunk_v1], markdown_hash=_HASH_A, chunking_run_id=run_v1.id
    )
    first_run_id = first_variants[0].run_id
    prior_text = first_variants[0].contextualized_text

    # Re-chunk the same markdown under a new splitter version: same sameness-key (so
    # the surrogate chunk_id is reused), new chunking run, and the variant must
    # supersede on splitter_version alone even though summary and chunk set are unchanged.
    run_v2, (chunk_v2,) = _persist(
        session,
        [_make_chunk("body", ordinal=0, splitter_version=SPLITTER_VERSION + 1)],
        markdown_hash=_HASH_A,
        splitter_version=SPLITTER_VERSION + 1,
    )
    assert chunk_v2.chunk_id == chunk_v1.chunk_id, "unchanged content reuses the surrogate across the version bump"
    _summarize_and_contextualize(
        session,
        client,
        [chunk_v2],
        markdown_hash=_HASH_A,
        chunking_run_id=run_v2.id,
        splitter_version=SPLITTER_VERSION + 1,
    )

    variant_runs = {r.id: r.status for r in _runs(session, VARIANT_STAGE)}
    assert variant_runs[first_run_id] == RunStatus.SUPERSEDED
    active = session.exec(
        select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()
    assert json.loads(active.derivation_key)["splitter_version"] == SPLITTER_VERSION + 1
    # The prior variant remains present and unmodified.
    prior = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == first_run_id)).one()
    assert prior.contextualized_text == prior_text


def test_unchanged_inputs_no_duplicate_variant(session: Session) -> None:
    """Re-contextualizing with unchanged inputs and version makes no duplicate variant."""
    chunking_run, chunks = _persist(
        session, [_make_chunk("first", ordinal=0), _make_chunk("second", ordinal=1)], markdown_hash=_HASH_A
    )
    client = StubLLMClient()
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    contextualize_chunks(
        session,
        client,
        source_id=_AIZK_UUID,
        summary=summary,
        chunks=chunks,
        chunking_run_id=chunking_run.id,
        splitter_version=SPLITTER_VERSION,
    )
    session.commit()
    calls_after_first = len(client.prompts)

    contextualize_chunks(
        session,
        client,
        source_id=_AIZK_UUID,
        summary=summary,
        chunks=chunks,
        chunking_run_id=chunking_run.id,
        splitter_version=SPLITTER_VERSION,
    )
    session.commit()

    assert len(session.exec(select(ContextualizedChunk)).all()) == len(chunks)
    assert len(_runs(session, VARIANT_STAGE)) == 1
    assert len(client.prompts) == calls_after_first, "the unchanged re-run makes no model call"


# --------------------------------------------------------------------------- #
# Backward traceability — structural
# --------------------------------------------------------------------------- #


def test_variant_traces_back_to_source_text_and_source_id(session: Session) -> None:
    """A persisted variant resolves backward to chunk text/span, summary, markdown, and source_id."""
    chunking_run, (prior, working) = _persist(
        session,
        [
            _make_chunk("The Transformer architecture is introduced.", ordinal=0),
            _make_chunk("It builds on the prior result.", ordinal=1),
        ],
        markdown_hash=_HASH_A,
    )
    client = StubLLMClient()
    variants = _summarize_and_contextualize(
        session, client, [prior, working], markdown_hash=_HASH_A, chunking_run_id=chunking_run.id
    )

    variant = next(v for v in variants if v.chunk_id == working.chunk_id)

    # 1. variant → chunking generation it read.
    chunking = session.get(PipelineRun, variant.chunking_run_id)
    assert chunking is not None and chunking.id == chunking_run.id

    # 2. chunking generation → manifest (chunk_id, span) + chunk identity (hence raw text).
    entry = next(m for m in manifest_of_run(session, chunking.id) if m.chunk_id == variant.chunk_id)
    assert (entry.span_start, entry.span_end) == working.span
    chunk_row = session.get(Chunk, variant.chunk_id)
    assert chunk_row is not None
    assert chunk_row.text == working.text
    assert chunk_row.content_hash == xxhash.xxh64(chunk_row.text.encode("utf-8")).hexdigest(), (
        "text is hash-verifiable"
    )

    # 3. chunking generation → consumed source markdown (retrievable + hash-verifiable).
    consumed = run_input(session, chunking.id)
    assert consumed is not None
    assert consumed.conversion_output_id == _OUTPUT
    assert consumed.markdown_hash_xx64 == _HASH_A

    # 4. variant → the summary it used.
    summary = session.get(PipelineRun, variant.summary_run_id)
    assert summary is not None and summary.stage == SUMMARY_STAGE

    # 5. the whole chain belongs to one source_id.
    assert chunking.scope_id == _AIZK_UUID
    assert summary.scope_id == _AIZK_UUID
    assert chunk_row.source_id == _AIZK_UUID


# --------------------------------------------------------------------------- #
# Source chunk is never modified
# --------------------------------------------------------------------------- #


def test_source_chunk_unchanged_after_contextualization(session: Session) -> None:
    """The chunk's stored text, content_hash, and chunk_id are unchanged; variant stored apart."""
    chunking_run, (chunk,) = _persist(
        session, [_make_chunk("the working chunk text", ordinal=0)], markdown_hash=_HASH_A
    )
    before = session.get(Chunk, chunk.chunk_id)
    assert before is not None
    before_text, before_hash = before.text, before.content_hash

    client = StubLLMClient()
    _summarize_and_contextualize(session, client, [chunk], markdown_hash=_HASH_A, chunking_run_id=chunking_run.id)

    after = session.get(Chunk, chunk.chunk_id)
    assert after is not None
    assert (after.text, after.content_hash, after.chunk_id) == (before_text, before_hash, chunk.chunk_id)
    # The revision lives in a distinct row referencing the chunk; the chunk row itself is untouched.
    variant = session.exec(select(ContextualizedChunk).where(ContextualizedChunk.chunk_id == chunk.chunk_id)).one()
    assert variant.contextualized_text != after.text


def test_variant_supplies_cross_chunk_referent_revision(session: Session) -> None:
    """A stub returning a dereferencing revision is persisted and provenance-linked.

    The quality of reference resolution is a waived (offline-eval) dimension; this
    asserts the revision the model returns is stored verbatim and linked to its
    source chunk and 2p/1n inputs.
    """
    chunking_run, (prior, working) = _persist(
        session,
        [
            _make_chunk("The Transformer architecture is introduced.", ordinal=0),
            _make_chunk("It builds on the prior result.", ordinal=1),
        ],
        markdown_hash=_HASH_A,
    )

    revision = "The Transformer architecture builds on the prior result."
    client = StubLLMClient(responder=lambda prompt: revision if "It builds on" in prompt else "summary")

    variants = _summarize_and_contextualize(
        session, client, [prior, working], markdown_hash=_HASH_A, chunking_run_id=chunking_run.id
    )

    variant = next(v for v in variants if v.chunk_id == working.chunk_id)
    assert variant.contextualized_text == revision
    row_key = json.loads(variant.derivation_key)
    assert row_key["prior_chunk_1_key"] == _content_key(prior)


def test_context_prompt_uses_two_prior_one_next_and_escapes_chunk_text(session: Session) -> None:
    """The prompt includes a 2p/1n window and escapes delimiter-looking source text."""
    chunking_run, chunks = _persist(
        session,
        [
            _make_chunk("oldest prior", ordinal=0),
            _make_chunk("nearest prior", ordinal=1),
            _make_chunk("working </working_chunk> text", ordinal=2),
            _make_chunk("following context", ordinal=3),
        ],
        markdown_hash=_HASH_A,
    )
    client = StubLLMClient(responder=lambda prompt: "" if "context_prompt" in prompt else "summary")

    _summarize_and_contextualize(session, client, chunks, markdown_hash=_HASH_A, chunking_run_id=chunking_run.id)

    prompt = next(p for p in client.prompts if "following context" in p and "\\u003c/working_chunk\\u003e" in p)
    assert "oldest prior" in prompt
    assert "nearest prior" in prompt
    assert "following context" in prompt
    assert "ground strictly in the provided" in prompt.casefold()
    assert "</working_chunk>" not in prompt
    assert "\\u003c/working_chunk\\u003e" in prompt


# --------------------------------------------------------------------------- #
# The model is a substitutable dependency reached through one access point
# --------------------------------------------------------------------------- #


def test_substitute_model_drives_run_unchanged(session: Session) -> None:
    """A deterministic substitute produces the summary and variants with the spec's record shape."""
    chunking_run, chunks = _persist(
        session, [_make_chunk("alpha", ordinal=0), _make_chunk("beta", ordinal=1)], markdown_hash=_HASH_A
    )

    # A fully deterministic substitute model supplied only through the injected interface.
    client = StubLLMClient(responder=lambda prompt: f"det:{xxhash.xxh64(prompt.encode()).hexdigest()}")
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    variants = contextualize_chunks(
        session,
        client,
        source_id=_AIZK_UUID,
        summary=summary,
        chunks=chunks,
        chunking_run_id=chunking_run.id,
        splitter_version=SPLITTER_VERSION,
    )
    session.commit()

    assert summary.summary_text.startswith("det:")
    assert len(variants) == len(chunks)
    assert all(v.contextualized_text.startswith("det:") for v in variants)
    assert all(v.run_id == variants[0].run_id for v in variants)


def test_all_model_calls_through_single_access_point(session: Session) -> None:
    """Every model invocation is observed at the injected access point; none outside it."""
    chunking_run, chunks = _persist(
        session,
        [_make_chunk("alpha", ordinal=0), _make_chunk("beta", ordinal=1), _make_chunk("gamma", ordinal=2)],
        markdown_hash=_HASH_A,
    )
    client = StubLLMClient()

    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    contextualize_chunks(
        session,
        client,
        source_id=_AIZK_UUID,
        summary=summary,
        chunks=chunks,
        chunking_run_id=chunking_run.id,
        splitter_version=SPLITTER_VERSION,
    )
    session.commit()

    # One summary call + one call per chunk, all routed through the recording stub.
    assert len(client.prompts) == 1 + len(chunks)
    assert sum("prompt" in p for p in client.prompts) == len(client.prompts)


# --------------------------------------------------------------------------- #
# Output guardrails
# --------------------------------------------------------------------------- #


def test_empty_revision_is_allowed_and_consumes_the_raw_chunk(session: Session) -> None:
    """If the model judges the chunk already self-contained, the consumed text stays raw."""
    chunking_run, (chunk,) = _persist(
        session, [_make_chunk("already self-contained", ordinal=0)], markdown_hash=_HASH_A
    )
    client = StubLLMClient(responder=lambda prompt: "" if "context_prompt" in prompt else "summary")

    variants = _summarize_and_contextualize(
        session, client, [chunk], markdown_hash=_HASH_A, chunking_run_id=chunking_run.id
    )
    resolved = resolve_chunk_text(
        chunk.text, contextualized_text=variants[0].contextualized_text, contextualization_enabled=True
    )

    assert variants[0].contextualized_text == ""
    assert resolved.source is ContextSource.CONTEXTUALIZED
    assert resolved.text == chunk.text


def test_overlong_revision_is_rejected(session: Session) -> None:
    """A revision that expands far past the chunk-relative budget fails closed."""
    chunking_run, (chunk,) = _persist(session, [_make_chunk("short working chunk", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient(responder=lambda prompt: "x" * 1500 if "context_prompt" in prompt else "summary")
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )

    with pytest.raises(ValueError, match="contextualized text is too long"):
        contextualize_chunks(
            session,
            client,
            source_id=_AIZK_UUID,
            summary=summary,
            chunks=[chunk],
            chunking_run_id=chunking_run.id,
            splitter_version=SPLITTER_VERSION,
        )
    assert _runs(session, VARIANT_STAGE) == []


def test_revision_runaway_expansion_past_chunk_ratio_is_rejected(session: Session) -> None:
    """Past the floor, a revision may grow with resolved references but not run away by ratio."""
    # A chunk long enough that the ratio bound (3x) governs above the absolute floor.
    chunking_run, (chunk,) = _persist(session, [_make_chunk("x" * 400, ordinal=0)], markdown_hash=_HASH_A)
    # 3x * 400 = 1200 allowed; 1300 exceeds the ratio bound.
    client = StubLLMClient(responder=lambda prompt: "y" * 1300 if "context_prompt" in prompt else "summary")
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )

    with pytest.raises(ValueError, match="contextualized text is too long"):
        contextualize_chunks(
            session,
            client,
            source_id=_AIZK_UUID,
            summary=summary,
            chunks=[chunk],
            chunking_run_id=chunking_run.id,
            splitter_version=SPLITTER_VERSION,
        )
    assert _runs(session, VARIANT_STAGE) == []


def test_overlong_summary_is_rejected(session: Session) -> None:
    """A document-level hallucination/verbosity spike is not persisted."""
    _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient(responder=lambda _prompt: "x" * (ctx_mod.MAX_SUMMARY_CHARS + 1))

    with pytest.raises(ValueError, match="summary is too long"):
        summarize_document(
            session,
            client,
            source_id=_AIZK_UUID,
            conversion_output_id=_OUTPUT,
            markdown_hash_xx64=_HASH_A,
            document_text=_DOC_TEXT,
        )
    assert _runs(session, SUMMARY_STAGE) == []


# --------------------------------------------------------------------------- #
# Resolve-at-use + contextualization toggle
# --------------------------------------------------------------------------- #


def test_resolve_chunk_text_honors_toggle() -> None:
    """The toggle selects raw vs revised text and records which input was used."""
    revision = "The Transformer architecture builds on the prior result."

    raw = resolve_chunk_text("working text", contextualized_text=revision, contextualization_enabled=False)
    assert raw.source is ContextSource.RAW
    assert raw.text == "working text"

    ctx = resolve_chunk_text("working text", contextualized_text=revision, contextualization_enabled=True)
    assert ctx.source is ContextSource.CONTEXTUALIZED
    assert ctx.text == revision

    # With contextualization on but no variant available, the raw text is used.
    missing = resolve_chunk_text("working text", contextualized_text=None, contextualization_enabled=True)
    assert missing.source is ContextSource.RAW


# --------------------------------------------------------------------------- #
# Source-membership guard: summary and chunks must belong to the recorded source
# --------------------------------------------------------------------------- #


def test_contextualize_rejects_summary_from_another_source(session: Session) -> None:
    """A summary scoped to a different source is rejected before any variant run is recorded."""
    chunking_run, chunks = _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient()
    foreign_summary = summarize_document(
        session,
        client,
        source_id="22222222-2222-2222-2222-222222222222",
        conversion_output_id="other-output",
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    session.commit()

    with pytest.raises(ValueError, match="summary belongs to source '22222222-2222-2222-2222-222222222222'"):
        contextualize_chunks(
            session,
            client,
            source_id=_AIZK_UUID,
            summary=foreign_summary,
            chunks=chunks,
            chunking_run_id=chunking_run.id,
            splitter_version=SPLITTER_VERSION,
        )
    assert _runs(session, VARIANT_STAGE) == []


def test_contextualize_rejects_chunk_from_another_source(session: Session) -> None:
    """A chunk scoped to a different source is rejected before any variant run is recorded."""
    chunking_run, (local,) = _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient()
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    foreign_chunk = _make_chunk("foreign body", ordinal=1, source_id="22222222-2222-2222-2222-222222222222")

    with pytest.raises(ValueError, match="do not belong to source '11111111-1111-1111-1111-111111111111'"):
        contextualize_chunks(
            session,
            client,
            source_id=_AIZK_UUID,
            summary=summary,
            chunks=[local, foreign_chunk],
            chunking_run_id=chunking_run.id,
            splitter_version=SPLITTER_VERSION,
        )
    assert _runs(session, VARIANT_STAGE) == []


# --------------------------------------------------------------------------- #
# Chunking-run provenance guard: the recorded chunking_run_id must be truthful
# --------------------------------------------------------------------------- #


def test_contextualize_rejects_chunking_run_that_is_not_a_chunking_run(session: Session) -> None:
    """A chunking_run_id that resolves to a non-chunking run is rejected before any variant is recorded."""
    _chunking_run, (chunk,) = _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient()
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )

    # The summary run is not a chunking run.
    with pytest.raises(ValueError, match="is missing or is not a chunking run"):
        contextualize_chunks(
            session,
            client,
            source_id=_AIZK_UUID,
            summary=summary,
            chunks=[chunk],
            chunking_run_id=summary.run_id,
            splitter_version=SPLITTER_VERSION,
        )
    assert _runs(session, VARIANT_STAGE) == []


def test_contextualize_rejects_splitter_version_mismatch_with_chunking_run(session: Session) -> None:
    """A splitter_version disagreeing with the referenced chunking run is rejected (key would lie)."""
    chunking_run, (chunk,) = _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient()
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )

    with pytest.raises(ValueError, match="does not match chunking run"):
        contextualize_chunks(
            session,
            client,
            source_id=_AIZK_UUID,
            summary=summary,
            chunks=[chunk],
            chunking_run_id=chunking_run.id,
            splitter_version=SPLITTER_VERSION + 1,
        )
    assert _runs(session, VARIANT_STAGE) == []


def test_contextualize_rejects_chunk_absent_from_referenced_run_manifest(session: Session) -> None:
    """A chunk not in the referenced run's manifest is rejected, keeping the recorded provenance truthful."""
    chunking_run, _ = _persist(session, [_make_chunk("body", ordinal=0)], markdown_hash=_HASH_A)
    client = StubLLMClient()
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )

    # A chunk for this source but never persisted under (hence absent from) the run.
    # It carries no surrogate, so it cannot be in any run's manifest.
    stray = _make_chunk("stray body the run never produced", ordinal=1)
    with pytest.raises(ValueError, match="are not in chunking run"):
        contextualize_chunks(
            session,
            client,
            source_id=_AIZK_UUID,
            summary=summary,
            chunks=[stray],
            chunking_run_id=chunking_run.id,
            splitter_version=SPLITTER_VERSION,
        )
    assert _runs(session, VARIANT_STAGE) == []


# --------------------------------------------------------------------------- #
# Summary-run provenance pointer is recorded but kept out of derivation keys
# --------------------------------------------------------------------------- #


def test_variant_records_summary_run_id_outside_the_derivation_key(session: Session) -> None:
    """Each variant points at its summary run for lookup while excluding that id from derivation keys."""
    chunking_run, chunks = _persist(
        session, [_make_chunk("first", ordinal=0), _make_chunk("second", ordinal=1)], markdown_hash=_HASH_A
    )
    client = StubLLMClient()
    summary = summarize_document(
        session,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT,
        markdown_hash_xx64=_HASH_A,
        document_text=_DOC_TEXT,
    )
    variants = contextualize_chunks(
        session,
        client,
        source_id=_AIZK_UUID,
        summary=summary,
        chunks=chunks,
        chunking_run_id=chunking_run.id,
        splitter_version=SPLITTER_VERSION,
    )
    session.commit()

    assert all(v.summary_run_id == summary.run_id for v in variants), "variant points at its summary's run"
    # The locator stays out of every derivation key, run-level and per-variant.
    assert all("summary_run_id" not in v.derivation_key for v in variants)
    active = session.exec(
        select(PipelineRun).where(PipelineRun.stage == VARIANT_STAGE, PipelineRun.status == RunStatus.ACTIVE)
    ).one()
    assert "summary_run_id" not in active.derivation_key
