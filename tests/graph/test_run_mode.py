"""Behavioral tests for the graph unit-of-work and its two enqueue entry points.

Exercises the `chunk-contextualization` run-mode-independence contract through
:mod:`aizk.graph.workunit`: the same document processed after a bulk/backfill
enqueue and after an incremental enqueue yields the same run records — identical
chunking/summary/variant derivation keys, provenance linkage, and consumed-input
locators — because both modes feed the one :func:`process_document` write path.
Also covers enqueue dedupe, the markdown-hash integrity guard, and re-execution
idempotency of the unit-of-work.

Each test builds its own file-based SQLite database so the two modes are compared
across independent stores (run ids differ; the content-derived derivation keys do
not). A deterministic stub Markdown source, freshness, and LLM client keep the
records reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from aizk.graph.content_index import CONTENT_FTS_DDL
from aizk.graph.datamodel import ContextualizationJob, ContextualizedChunk, DocumentSummary
from aizk.graph.llm import StubLLMClient
from aizk.graph.persistence import manifest_of_run, run_input
from aizk.graph.workunit import (
    LoadedMarkdown,
    ProcessResult,
    enqueue_backfill,
    enqueue_document,
    process_document,
)
from aizk.pipeline.lifecycle import WorkUnitStatus
from aizk.pipeline.run import PipelineRun, RunStatus
from aizk.utilities.hashing import compute_markdown_hash

_AIZK_UUID = UUID("11111111-1111-1111-1111-111111111111")
_OUTPUT_ID = 42
_OTHER_AIZK_UUID = UUID("22222222-2222-2222-2222-222222222222")
_OTHER_OUTPUT_ID = 7

_MARKDOWN = """# Title

A paragraph under the title with enough text to be a real chunk.

## Section One

Content for the first section that the splitter carves into a chunk.

## Section Two

Content for the second section, distinct from the first.
"""


@dataclass
class _StubMarkdownSource:
    """A deterministic :class:`~aizk.graph.workunit.MarkdownSource` for tests."""

    text: str

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        """Return the stub Markdown and its computed hash for any locator."""
        return LoadedMarkdown(text=self.text, markdown_hash_xx64=compute_markdown_hash(self.text))


class _AlwaysCurrent:
    """A freshness stub treating every output as the source's latest."""

    def is_current(self, session: Session, source_id: UUID, conversion_output_id: int) -> bool:
        return True


def _make_engine(tmp_path: Path, name: str):
    """Create a file-based SQLite engine with the full registered schema."""
    engine = create_engine(f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(CONTENT_FTS_DDL))
    return engine


def _run(engine, *, conversion_output_id: int = _OUTPUT_ID, source_id: UUID = _AIZK_UUID) -> ProcessResult:
    """Run the unit-of-work for one document and return its (non-skipped) result."""
    result = process_document(
        engine,
        StubLLMClient(),
        source_id=source_id,
        conversion_output_id=conversion_output_id,
        markdown_source=_StubMarkdownSource(_MARKDOWN),
        freshness=_AlwaysCurrent(),
    )
    assert isinstance(result, ProcessResult)
    return result


def _snapshot(engine, source_id: UUID = _AIZK_UUID) -> dict:
    """Reduce a source's active run records to a DB-independent comparable form."""
    scope = str(source_id)
    with Session(engine) as session:
        active = {
            r.stage: r
            for r in session.exec(
                select(PipelineRun).where(PipelineRun.scope_id == scope, PipelineRun.status == RunStatus.ACTIVE)
            ).all()
        }
        chunking, summary_run, variant_run = (
            active.get("chunking"),
            active.get("document_summary"),
            active.get("chunk_contextualization"),
        )
        consumed = run_input(session, chunking.id)
        summary = session.exec(select(DocumentSummary).where(DocumentSummary.run_id == summary_run.id)).one()
        # Sort and key by the portable derivation key, never the surrogate chunk_id,
        # which is DB-local (each database mints its own UUIDs).
        variants = sorted(
            session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)).all(),
            key=lambda v: v.derivation_key,
        )
        return {
            "chunking_derivation_key": chunking.derivation_key,
            "chunking_version_stamps": chunking.version_stamps_json,
            "consumed_input": (consumed.conversion_output_id, consumed.markdown_hash_xx64),
            "manifest": sorted((m.span_start, m.span_end) for m in manifest_of_run(session, chunking.id)),
            "summary_derivation_key": summary_run.derivation_key,
            "summary_record": (summary.conversion_output_id, summary.markdown_hash_xx64, summary.summary_version),
            "variant_run_derivation_key": variant_run.derivation_key,
            "variant_rows": [v.derivation_key for v in variants],
            "variant_count": len(variants),
            "provenance_linked": all(
                v.summary_run_id == summary_run.id and v.chunking_run_id == chunking.id for v in variants
            ),
        }


def _process(engine, *, mode: str) -> dict:
    """Enqueue the document in ``mode``, run the unit-of-work, and return its snapshot."""
    with Session(engine) as session:
        if mode == "bulk":
            enqueue_backfill(session, [(_OTHER_OUTPUT_ID, _OTHER_AIZK_UUID), (_OUTPUT_ID, _AIZK_UUID)], confirmed=True)
        else:
            enqueue_document(session, conversion_output_id=_OUTPUT_ID, source_id=_AIZK_UUID)
        session.commit()
    _run(engine)
    return _snapshot(engine)


def test_bulk_and_incremental_same_records(tmp_path: Path) -> None:
    """The same document in bulk and incremental modes yields identical run records."""
    bulk = _process(_make_engine(tmp_path, "bulk.db"), mode="bulk")
    incremental = _process(_make_engine(tmp_path, "incremental.db"), mode="incremental")

    # Guard against a vacuous equality (empty == empty).
    assert bulk["variant_count"] >= 2
    assert bulk["provenance_linked"] is True
    assert bulk == incremental


def test_incremental_matches_bulk_shape(tmp_path: Path) -> None:
    """A single incrementally-ingested document has the expected one-summary / one-variant-per-chunk shape."""
    engine = _make_engine(tmp_path, "single.db")
    snap = _process(engine, mode="incremental")

    assert snap["variant_count"] == len(snap["manifest"])
    assert snap["variant_count"] >= 1
    assert snap["provenance_linked"] is True
    assert snap["consumed_input"] == (str(_OUTPUT_ID), compute_markdown_hash(_MARKDOWN))
    assert snap["summary_record"][0] == str(_OUTPUT_ID)
    assert snap["chunking_derivation_key"] and snap["variant_run_derivation_key"]


def test_enqueue_dedupes_on_idempotency_key(tmp_path: Path) -> None:
    """Re-enqueueing the same conversion output reuses the open work-unit, in either mode."""
    engine = _make_engine(tmp_path, "dedupe.db")
    with Session(engine) as session:
        first = enqueue_document(session, conversion_output_id=_OUTPUT_ID, source_id=_AIZK_UUID)
        session.commit()
        first_id = first.id

        again = enqueue_document(session, conversion_output_id=_OUTPUT_ID, source_id=_AIZK_UUID)
        (backfilled,) = enqueue_backfill(session, [(_OUTPUT_ID, _AIZK_UUID)], confirmed=True)
        session.commit()

        assert again.id == first_id
        assert backfilled.id == first_id
        rows = session.exec(
            select(ContextualizationJob).where(ContextualizationJob.conversion_output_id == _OUTPUT_ID)
        ).all()
        assert len(rows) == 1
        assert rows[0].status is WorkUnitStatus.QUEUED


def test_process_document_is_idempotent(tmp_path: Path) -> None:
    """Re-running the unit-of-work reuses the active runs and creates no duplicate records."""
    engine = _make_engine(tmp_path, "idempotent.db")
    first = _run(engine)
    second = _run(engine)

    assert second.chunking_run_id == first.chunking_run_id
    assert second.summary_run_id == first.summary_run_id
    with Session(engine) as session:
        assert len(session.exec(select(PipelineRun)).all()) == 3
        assert len(session.exec(select(DocumentSummary)).all()) == 1
        assert len(session.exec(select(ContextualizedChunk)).all()) == first.variant_count


def test_process_document_rejects_markdown_hash_mismatch(tmp_path: Path) -> None:
    """A fetched Markdown that disagrees with its recorded hash fails closed before persistence."""
    engine = _make_engine(tmp_path, "drift.db")

    class _DriftingSource:
        def load(self, conversion_output_id: int) -> LoadedMarkdown:
            return LoadedMarkdown(text=_MARKDOWN, markdown_hash_xx64="deadbeefdeadbeef")

    with pytest.raises(ValueError, match="does not hash to|hashes to"):
        process_document(
            engine,
            StubLLMClient(),
            source_id=_AIZK_UUID,
            conversion_output_id=_OUTPUT_ID,
            markdown_source=_DriftingSource(),
            freshness=_AlwaysCurrent(),
        )
    with Session(engine) as session:
        assert session.exec(select(PipelineRun)).all() == []


def test_process_document_reuses_summary_text_on_rerun(tmp_path: Path) -> None:
    """A rerun reuses the persisted summary text (no second summary model call).

    Confirms process_document wires resolve_summary_text in: with the summary
    reused, the revisions a later run generates derive from the persisted summary,
    keeping a variant's recorded summary provenance consistent with its text.
    """
    engine = _make_engine(tmp_path, "summary_reuse.db")
    client = StubLLMClient()  # accumulates prompts across both runs
    source = _StubMarkdownSource(_MARKDOWN)

    process_document(
        engine,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT_ID,
        markdown_source=source,
        freshness=_AlwaysCurrent(),
    )
    summary_calls_after_first = sum("summary_prompt" in p for p in client.prompts)

    process_document(
        engine,
        client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT_ID,
        markdown_source=source,
        freshness=_AlwaysCurrent(),
    )
    summary_calls_after_second = sum("summary_prompt" in p for p in client.prompts)

    assert summary_calls_after_first == 1
    assert summary_calls_after_second == 1, "the summary is reused on rerun, not regenerated"


def test_variants_regenerate_under_reused_summary_use_persisted_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When variants regenerate but the summary is reused, the revisions derive from the persisted summary.

    Bumping the splitter version supersedes the chunking and variant runs (the
    splitter version folds into both derivation keys) while leaving the summary
    derivation key unchanged. The rerun must therefore reuse the persisted summary
    (no second summary model call) and condition every regenerated revision on that
    persisted summary text — so a regenerated variant's recorded summary provenance
    stays consistent with the text its revision was built from.
    """
    from aizk.chunking import SPLITTER_VERSION

    engine = _make_engine(tmp_path, "variant_regen.db")

    _run(engine)  # first run, at the baseline splitter version
    with Session(engine) as session:
        summary_text = session.exec(select(DocumentSummary.summary_text)).one()
        first_variant_run_id = session.exec(
            select(PipelineRun.id).where(
                PipelineRun.stage == "chunk_contextualization", PipelineRun.status == RunStatus.ACTIVE
            )
        ).one()

    # Bump the splitter version faithfully: the splitter stamps the new version onto
    # its chunks, and the write path records it. Chunking + variant runs supersede
    # (the splitter version folds into both derivation keys); the summary does not
    # (its key is markdown hash + summary version + prompt + profile).
    monkeypatch.setattr("aizk.chunking.splitter.SPLITTER_VERSION", SPLITTER_VERSION + 1)
    monkeypatch.setattr("aizk.graph.workunit.SPLITTER_VERSION", SPLITTER_VERSION + 1)

    rerun_client = StubLLMClient()  # a fresh recorder: only the rerun's prompts
    process_document(
        engine,
        rerun_client,
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT_ID,
        markdown_source=_StubMarkdownSource(_MARKDOWN),
        freshness=_AlwaysCurrent(),
    )

    # The summary was reused — the rerun made no summary call.
    assert not any("summary_prompt" in p for p in rerun_client.prompts)
    # The variants were regenerated — the rerun made contextualization calls...
    context_prompts = [p for p in rerun_client.prompts if "context_prompt" in p]
    assert context_prompts, "the splitter bump must regenerate variants"
    # ...and every regenerated revision was conditioned on the persisted summary text.
    assert all(summary_text in p for p in context_prompts)

    with Session(engine) as session:
        # The variant run actually superseded (a new active run id), confirming regeneration.
        second_variant_run_id = session.exec(
            select(PipelineRun.id).where(
                PipelineRun.stage == "chunk_contextualization", PipelineRun.status == RunStatus.ACTIVE
            )
        ).one()
        assert second_variant_run_id != first_variant_run_id
        # The reused summary remains a single row (no duplicate summary written).
        assert len(session.exec(select(DocumentSummary)).all()) == 1


def test_process_document_skips_writes_when_cancelled(tmp_path: Path) -> None:
    """A cancellation probe that fires makes the unit-of-work write nothing."""
    from aizk.graph.workunit import Cancelled

    engine = _make_engine(tmp_path, "cancelled.db")
    result = process_document(
        engine,
        StubLLMClient(),
        source_id=_AIZK_UUID,
        conversion_output_id=_OUTPUT_ID,
        markdown_source=_StubMarkdownSource(_MARKDOWN),
        freshness=_AlwaysCurrent(),
        is_cancelled=lambda: True,
    )
    assert isinstance(result, Cancelled)
    with Session(engine) as session:
        assert session.exec(select(PipelineRun)).all() == []
