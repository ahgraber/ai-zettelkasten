"""Behavioral tests for contextualization checkpoint-and-resume.

Exercises the ``chunk-contextualization`` resume contract: a retry of a
partially-completed attempt re-invokes the model only for outputs not already
retained (the summary and per-chunk revisions are memoized as validated model
output keyed by their input-deterministic derivation keys), only valid output is
eligible for reuse, and retained intermediate work is never observable as
committed contextualization state. Drives the deterministic stub
:class:`~aizk.graph.llm.StubLLMClient` and asserts on its recorded invocations and
on persisted records / memo rows.

These tests use a **plain** engine (no auto-``BEGIN IMMEDIATE`` listener) because
the memo-aware resolvers and the unit-of-work own their transaction boundaries via
:func:`aizk.graph.db.begin_immediate`; an auto-emitting listener would double-begin.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlmodel import Session, SQLModel, create_engine, select
import xxhash

from aizk.chunking import SPLITTER_VERSION, Chunk as SplitterChunk, split
from aizk.graph.content_index import CONTENT_FTS_DDL
from aizk.graph.contextualization import (
    MAX_SUMMARY_CHARS,
    StalePlanError,
    contextualize_chunks,
    resolve_revisions,
    resolve_summary_text,
    summarize_document,
)
from aizk.graph.datamodel import (
    MEMO_KIND_REVISION,
    MEMO_KIND_SUMMARY,
    ContextualizationOutputMemo,
    ContextualizedChunk,
    DocumentSummary,
)
from aizk.graph.handler import ContextualizationStageHandler
from aizk.graph.llm import StubLLMClient
import aizk.graph.persistence as persistence_mod
from aizk.graph.persistence import memo_get, persist_chunks
from aizk.graph.workunit import LoadedMarkdown, ProcessResult, process_document
from aizk.pipeline.lifecycle import RetryClass
from aizk.pipeline.run import PipelineRun, RunStatus
from aizk.utilities.hashing import compute_markdown_hash

_AIZK_UUID = UUID("11111111-1111-1111-1111-111111111111")
_OTHER_AIZK_UUID = UUID("22222222-2222-2222-2222-222222222222")
_SCOPE = str(_AIZK_UUID)
_OUTPUT_ID = 42
_HASH = "0011223344556677"
_DOC_TEXT = "# Title\n\nThe document body the summary pass reads."

_MARKDOWN = """# Title

A paragraph under the title with enough text to be a real chunk of content.

## Section One

Content for the first section that the splitter carves into its own chunk here.

## Section Two

Content for the second section, distinct from the first, its own chunk as well.

## Section Three

Content for a third section so the document yields several chunks to resume over.
"""


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A plain file-based SQLite engine with the full schema (no BEGIN-IMMEDIATE listener)."""
    eng = create_engine(
        f"sqlite:///{tmp_path / 'resume.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text(CONTENT_FTS_DDL))
    yield eng
    eng.dispose()


@dataclass
class _StubMarkdownSource:
    """A deterministic Markdown source returning fixed text with its true hash."""

    text: str

    def load(self, conversion_output_id: int) -> LoadedMarkdown:
        """Return the stub Markdown and its computed hash for any locator."""
        return LoadedMarkdown(text=self.text, markdown_hash_xx64=compute_markdown_hash(self.text))


class _AlwaysCurrent:
    """A freshness stub treating every output as the source's latest."""

    def is_current(self, session: Session, source_id: UUID, conversion_output_id: int) -> bool:
        """Treat every conversion output as current."""
        return True


def _chunk(text: str, ordinal: int, *, scope: str = _SCOPE, markdown_hash: str = _HASH) -> SplitterChunk:
    """Build a splitter chunk for a single source (no chunk_id — identity is assigned at persistence)."""
    content_hash = xxhash.xxh64(text.encode("utf-8")).hexdigest()
    return SplitterChunk(
        content_hash=content_hash,
        source_id=scope,
        heading_path=(),
        ordinal=ordinal,
        text=text,
        char_count=len(text),
        converted_artifact_id="out",
        markdown_hash_xx64=markdown_hash,
        span=(0, len(text)),
        splitter_version=SPLITTER_VERSION,
    )


def _summary_calls(client: StubLLMClient) -> list[str]:
    """Return the summary-pass prompts the client recorded."""
    return [p for p in client.prompts if "summary_prompt" in p]


def _context_calls(client: StubLLMClient) -> list[str]:
    """Return the per-chunk contextualization prompts the client recorded."""
    return [p for p in client.prompts if "context_prompt" in p]


def _responder(
    *, summary: str = "the document summary", fail_summary: bool = False, fail_markers: tuple[str, ...] = ()
):
    """Build a stub responder: fixed summary, ``rev::<hash>`` revisions, optional failures."""

    def respond(prompt: str) -> str:
        if "summary_prompt" in prompt:
            if fail_summary:
                raise RuntimeError("model failed on the summary")
            return summary
        for marker in fail_markers:
            if marker in prompt:
                raise RuntimeError(f"model failed on chunk containing {marker!r}")
        return f"rev::{xxhash.xxh64(prompt.encode('utf-8')).hexdigest()}"

    return respond


def _fail_after_k_context(k: int, *, summary: str = "the document summary"):
    """Build a responder that raises on the ``(k+1)``-th contextualization call."""
    state = {"ctx": 0}

    def respond(prompt: str) -> str:
        if "summary_prompt" in prompt:
            return summary
        state["ctx"] += 1
        if state["ctx"] > k:
            raise RuntimeError(f"model failed on context call {state['ctx']}")
        return f"rev-{state['ctx']}"

    return respond


def _memo_rows(engine: Engine, scope: str, kind: str | None = None) -> list[ContextualizationOutputMemo]:
    """Return the memo rows for a source, optionally filtered by kind."""
    with Session(engine) as session:
        statement = select(ContextualizationOutputMemo).where(ContextualizationOutputMemo.scope_id == scope)
        if kind is not None:
            statement = statement.where(ContextualizationOutputMemo.kind == kind)
        return list(session.exec(statement).all())


def _active_runs(engine: Engine, scope: str) -> dict[str, PipelineRun]:
    """Return the active runs for a source keyed by stage."""
    with Session(engine) as session:
        return {
            r.stage: r
            for r in session.exec(
                select(PipelineRun).where(PipelineRun.scope_id == scope, PipelineRun.status == RunStatus.ACTIVE)
            ).all()
        }


def _process(
    engine: Engine,
    client: StubLLMClient,
    *,
    source_id: UUID = _AIZK_UUID,
    conversion_output_id: int = _OUTPUT_ID,
    markdown: str = _MARKDOWN,
) -> ProcessResult:
    """Run the unit-of-work for one document and return its (non-skipped) result."""
    result = process_document(
        engine,
        client,
        source_id=source_id,
        conversion_output_id=conversion_output_id,
        markdown_source=_StubMarkdownSource(markdown),
        freshness=_AlwaysCurrent(),
    )
    assert isinstance(result, ProcessResult)
    return result


def _variant_snapshot(engine: Engine, scope: str) -> dict[str, object]:
    """Reduce a source's active generation to a DB-independent comparable form."""
    active = _active_runs(engine, scope)
    chunking, summary_run, variant_run = (
        active["chunking"],
        active["document_summary"],
        active["chunk_contextualization"],
    )
    with Session(engine) as session:
        summary = session.exec(select(DocumentSummary).where(DocumentSummary.run_id == summary_run.id)).one()
        # Sort and key by the portable derivation key, never the surrogate chunk_id,
        # so the snapshot is comparable across databases (each mints its own surrogates).
        variants = sorted(
            session.exec(select(ContextualizedChunk).where(ContextualizedChunk.run_id == variant_run.id)).all(),
            key=lambda v: v.derivation_key,
        )
        return {
            "summary_text": summary.summary_text,
            "summary_derivation_key": summary_run.derivation_key,
            "variant_run_derivation_key": variant_run.derivation_key,
            "chunking_derivation_key": chunking.derivation_key,
            "variant_count": len(variants),
            "variant_keys": [(v.derivation_key, v.contextualized_text) for v in variants],
        }


# --------------------------------------------------------------------------- #
# Generation-phase reuse (requirement 1)
# --------------------------------------------------------------------------- #


def test_first_contextualization_summary_is_memoized_and_reused_with_zero_summary_calls(engine: Engine) -> None:
    """A first-contextualization summary is retained and reused on retry with no summary model call."""
    first = StubLLMClient(responder=_responder(summary="summary-one"))
    summary_text = resolve_summary_text(
        engine, first, source_id=_SCOPE, markdown_hash_xx64=_HASH, document_text=_DOC_TEXT
    )
    assert summary_text == "summary-one"
    assert len(_summary_calls(first)) == 1
    assert memo_get_via(engine, MEMO_KIND_SUMMARY, _SCOPE, _summary_key()) == "summary-one"

    # A retry under unchanged inputs: a client that *would* differ must not be consulted.
    retry = StubLLMClient(responder=_responder(summary="summary-two"))
    reused = resolve_summary_text(engine, retry, source_id=_SCOPE, markdown_hash_xx64=_HASH, document_text=_DOC_TEXT)
    assert reused == "summary-one", "the retry reuses the retained summary, not a fresh one"
    assert retry.prompts == [], "no model call when the summary is memo-reused"


def test_per_chunk_revision_is_reused_from_the_memo_on_retry(engine: Engine) -> None:
    """A retained revision is reused on retry with no model call for that chunk."""
    chunks = [_chunk("first chunk body", 0), _chunk("second chunk body", 1)]
    first = StubLLMClient(responder=_responder())
    first_revisions = resolve_revisions(
        engine,
        first,
        source_id=_SCOPE,
        summary_text="the summary",
        markdown_hash_xx64=_HASH,
        ordered_chunks=chunks,
        splitter_version=SPLITTER_VERSION,
    )
    assert len(_context_calls(first)) == 2

    # Retry: a client that raises if consulted proves the revisions come from the memo.
    retry = StubLLMClient(responder=_responder(fail_markers=("first chunk body", "second chunk body")))
    reused = resolve_revisions(
        engine,
        retry,
        source_id=_SCOPE,
        summary_text="the summary",
        markdown_hash_xx64=_HASH,
        ordered_chunks=chunks,
        splitter_version=SPLITTER_VERSION,
    )
    assert reused == first_revisions, "retry yields the retained revisions verbatim"
    assert retry.prompts == [], "no model call when every revision is memo-reused"


def test_active_variant_run_precheck_skips_all_generation(engine: Engine) -> None:
    """After a completed generation, the precheck reuses the active run with zero revision calls."""
    _process(engine, StubLLMClient(responder=_responder()))
    summary_text = _variant_snapshot(engine, _SCOPE)["summary_text"]
    # Re-split to get the exact document-order chunks the persisted run was keyed on
    # (the splitter's ordinal ties across sections, so it is not a sort key).
    ordered = split(
        _MARKDOWN,
        source_id=_SCOPE,
        converted_artifact_id=str(_OUTPUT_ID),
        markdown_hash_xx64=compute_markdown_hash(_MARKDOWN),
    )

    # The success-path prune removed the revision memos, so without the precheck this
    # would re-invoke the model for every chunk. The precheck must return [] instead.
    probe = StubLLMClient(responder=_responder(fail_markers=tuple(c.text for c in ordered)))
    revisions = resolve_revisions(
        engine,
        probe,
        source_id=_SCOPE,
        summary_text=str(summary_text),
        markdown_hash_xx64=compute_markdown_hash(_MARKDOWN),
        ordered_chunks=ordered,
        splitter_version=SPLITTER_VERSION,
    )
    assert revisions is None, "a complete active variant run is signalled for reuse without generation"
    assert probe.prompts == [], "zero revision model calls when the active run matches"


# --------------------------------------------------------------------------- #
# Only valid output is reusable; empty is valid (requirement 2)
# --------------------------------------------------------------------------- #


def test_invalid_revision_is_not_retained_and_is_re_invoked_on_retry(engine: Engine) -> None:
    """An overlong revision is not memoized, so a retry re-invokes the model for that chunk."""
    working = _chunk("short", 0)
    row_key_probe = StubLLMClient(responder=lambda prompt: "x" * 5000 if "context_prompt" in prompt else "summary")
    with pytest.raises(ValueError, match="contextualized text is too long"):
        resolve_revisions(
            engine,
            row_key_probe,
            source_id=_SCOPE,
            summary_text="summary",
            markdown_hash_xx64=_HASH,
            ordered_chunks=[working],
            splitter_version=SPLITTER_VERSION,
        )
    assert _memo_rows(engine, _SCOPE, MEMO_KIND_REVISION) == [], "the invalid revision was not retained"

    # Retry with a valid output: the chunk is re-invoked and now succeeds.
    good = StubLLMClient(responder=_responder())
    revisions = resolve_revisions(
        engine,
        good,
        source_id=_SCOPE,
        summary_text="summary",
        markdown_hash_xx64=_HASH,
        ordered_chunks=[working],
        splitter_version=SPLITTER_VERSION,
    )
    assert len(_context_calls(good)) == 1, "the previously-invalid chunk is re-invoked"
    assert revisions[0].startswith("rev::")


def test_invalid_summary_is_not_retained_and_is_re_invoked_on_retry(engine: Engine) -> None:
    """An overlong summary is not memoized, so a retry re-invokes the summary pass."""
    overlong = StubLLMClient(responder=lambda _prompt: "x" * (MAX_SUMMARY_CHARS + 1))
    with pytest.raises(ValueError, match="summary is too long"):
        resolve_summary_text(engine, overlong, source_id=_SCOPE, markdown_hash_xx64=_HASH, document_text=_DOC_TEXT)
    assert _memo_rows(engine, _SCOPE, MEMO_KIND_SUMMARY) == [], "the invalid summary was not retained"

    good = StubLLMClient(responder=_responder(summary="valid summary"))
    resolved = resolve_summary_text(engine, good, source_id=_SCOPE, markdown_hash_xx64=_HASH, document_text=_DOC_TEXT)
    assert resolved == "valid summary"
    assert len(_summary_calls(good)) == 1, "the summary is re-invoked after the invalid attempt"


def test_empty_self_contained_revision_is_retained_and_reused(engine: Engine) -> None:
    """An empty revision is valid, retained, reused on retry, and persists as the empty variant."""
    chunks = [_chunk("already self-contained", 0)]
    first = StubLLMClient(responder=lambda prompt: "" if "context_prompt" in prompt else "summary")
    revisions = resolve_revisions(
        engine,
        first,
        source_id=_SCOPE,
        summary_text="summary",
        markdown_hash_xx64=_HASH,
        ordered_chunks=chunks,
        splitter_version=SPLITTER_VERSION,
    )
    assert revisions == [""], "an empty (self-contained) revision is a valid output"
    # Present-empty is a hit, not absence: the memo holds a row whose value is ''.
    assert memo_get_via(engine, MEMO_KIND_REVISION, _SCOPE, _revision_key(chunks[0])) == ""

    retry = StubLLMClient(responder=_responder(fail_markers=("already self-contained",)))
    reused = resolve_revisions(
        engine,
        retry,
        source_id=_SCOPE,
        summary_text="summary",
        markdown_hash_xx64=_HASH,
        ordered_chunks=chunks,
        splitter_version=SPLITTER_VERSION,
    )
    assert reused == [""]
    assert retry.prompts == [], "an already-self-contained chunk is not re-invoked"


# --------------------------------------------------------------------------- #
# The write transaction never spans a model call
# --------------------------------------------------------------------------- #


def test_no_memo_write_transaction_spans_a_model_call(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """No memo write transaction is open while the model is being called.

    Wraps the shared ``begin_immediate`` so a flag tracks whether a memo write
    transaction is open, and asserts inside every model invocation that it is not —
    so the generation phase would fail loudly if any upsert transaction were held
    across a model call.
    """
    flag = {"open": False}
    real = persistence_mod.begin_immediate

    @contextlib.contextmanager
    def tracking(eng: Engine) -> Iterator[Session]:
        flag["open"] = True
        try:
            with real(eng) as session:
                yield session
        finally:
            flag["open"] = False

    monkeypatch.setattr(persistence_mod, "begin_immediate", tracking)

    def asserting(prompt: str) -> str:
        assert not flag["open"], "a memo write transaction is open during a model call"
        return "summary" if "summary_prompt" in prompt else f"rev::{xxhash.xxh64(prompt.encode()).hexdigest()}"

    client = StubLLMClient(responder=asserting)
    chunks = [_chunk("alpha chunk", 0), _chunk("beta chunk", 1)]
    summary_text = resolve_summary_text(
        engine, client, source_id=_SCOPE, markdown_hash_xx64=_HASH, document_text=_DOC_TEXT
    )
    resolve_revisions(
        engine,
        client,
        source_id=_SCOPE,
        summary_text=summary_text,
        markdown_hash_xx64=_HASH,
        ordered_chunks=chunks,
        splitter_version=SPLITTER_VERSION,
    )
    assert len(_context_calls(client)) == 2, "both chunks were generated (none held a write lock)"


# --------------------------------------------------------------------------- #
# Success-path prune (requirement 3 / requirement 1)
# --------------------------------------------------------------------------- #


def test_success_prune_removes_consumed_keys_but_spares_unrelated(engine: Engine) -> None:
    """A successful generation deletes the keys it consumed, leaving an unrelated same-source key."""
    # Seed an unrelated memo row under the same source but a different derivation key.
    from aizk.graph.persistence import memo_upsert_and_read

    memo_upsert_and_read(engine, MEMO_KIND_REVISION, _SCOPE, "unrelated-key", "keep me")

    _process(engine, StubLLMClient(responder=_responder()))

    # The consumed summary + revision keys are gone; the unrelated row survives.
    assert _memo_rows(engine, _SCOPE, MEMO_KIND_SUMMARY) == [], "the consumed summary key was pruned"
    remaining = _memo_rows(engine, _SCOPE, MEMO_KIND_REVISION)
    assert [r.derivation_key for r in remaining] == ["unrelated-key"], "only the unrelated revision key remains"
    assert remaining[0].output_text == "keep me"


# --------------------------------------------------------------------------- #
# End-to-end resume behavior (requirements 1 and 3)
# --------------------------------------------------------------------------- #


def test_retry_resumes_and_matches_an_uninterrupted_run(tmp_path: Path) -> None:
    """A partial attempt that fails mid-revision resumes on retry, matching an uninterrupted run.

    The retry re-invokes the model only for the not-yet-revised chunks (not the
    summary, not the already-revised ones), and the persisted generation equals an
    uninterrupted run by count, derivation keys, and linkage.
    """
    # Baseline: an uninterrupted run on its own database.
    clean_engine = create_engine(f"sqlite:///{tmp_path / 'clean.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(clean_engine)
    with clean_engine.begin() as conn:
        conn.execute(text(CONTENT_FTS_DDL))
    _process(clean_engine, StubLLMClient(responder=_responder()))
    baseline = _variant_snapshot(clean_engine, _SCOPE)
    clean_engine.dispose()
    n = int(baseline["variant_count"])  # type: ignore[arg-type]
    assert n >= 2, "the fixture document must yield at least two chunks to resume over"

    # Resume: first attempt fails after the first chunk's revision; retry completes.
    resume_engine = create_engine(f"sqlite:///{tmp_path / 'resume_e2e.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(resume_engine)
    with resume_engine.begin() as conn:
        conn.execute(text(CONTENT_FTS_DDL))
    failing = StubLLMClient(responder=_fail_after_k_context(1))
    with pytest.raises(RuntimeError, match="model failed on context call"):
        process_document(
            resume_engine,
            failing,
            source_id=_AIZK_UUID,
            conversion_output_id=_OUTPUT_ID,
            markdown_source=_StubMarkdownSource(_MARKDOWN),
            freshness=_AlwaysCurrent(),
        )
    # Nothing persisted on the failed attempt.
    assert _active_runs(resume_engine, _SCOPE) == {}

    retry = StubLLMClient(responder=_responder())
    _process(resume_engine, retry)

    assert len(_summary_calls(retry)) == 0, "the summary is reused from the memo, not re-invoked"
    assert len(_context_calls(retry)) == n - 1, "only the not-yet-revised chunks are re-invoked"
    resumed = _variant_snapshot(resume_engine, _SCOPE)
    # The resumed generation matches an uninterrupted run by count, keys, and linkage
    # (the revision *texts* differ because the two runs used different stub responders).
    assert resumed["variant_count"] == baseline["variant_count"]
    assert resumed["summary_derivation_key"] == baseline["summary_derivation_key"]
    assert resumed["variant_run_derivation_key"] == baseline["variant_run_derivation_key"]
    # Compare the portable per-variant derivation keys (the texts differ by design).
    assert [k[0] for k in resumed["variant_keys"]] == [k[0] for k in baseline["variant_keys"]]  # type: ignore[index]
    resume_engine.dispose()


def test_completed_re_execution_invokes_the_model_zero_times(engine: Engine) -> None:
    """Re-executing an already-completed generation makes no model call and no duplicate records."""
    _process(engine, StubLLMClient(responder=_responder()))
    before = _variant_snapshot(engine, _SCOPE)
    runs_before = len(list(_all_runs(engine, _SCOPE)))

    rerun = StubLLMClient(responder=_responder())
    result = _process(engine, rerun)

    assert rerun.prompts == [], "a completed re-execution invokes the model zero times"
    assert len(list(_all_runs(engine, _SCOPE))) == runs_before, "no new run is created"
    assert _variant_snapshot(engine, _SCOPE) == before, "no duplicate summary or variant"
    assert result.variant_count == before["variant_count"]


def test_retained_work_for_one_source_is_not_reused_for_another(engine: Engine) -> None:
    """Two sources with byte-identical Markdown do not share retained model work."""
    # Source A: a partial attempt retains its summary (and some revisions), then fails.
    failing = StubLLMClient(responder=_fail_after_k_context(0))
    with pytest.raises(RuntimeError):
        process_document(
            engine,
            failing,
            source_id=_AIZK_UUID,
            conversion_output_id=_OUTPUT_ID,
            markdown_source=_StubMarkdownSource(_MARKDOWN),
            freshness=_AlwaysCurrent(),
        )
    assert _memo_rows(engine, _SCOPE, MEMO_KIND_SUMMARY), "source A retained its summary"

    # Source B: byte-identical Markdown, different source_id → must generate its own summary.
    source_b = StubLLMClient(responder=_responder())
    _process(engine, source_b, source_id=_OTHER_AIZK_UUID, conversion_output_id=99)
    assert len(_summary_calls(source_b)) == 1, "source B invokes the model for its own summary, not source A's"


def test_partial_failure_leaves_no_readable_generation(engine: Engine) -> None:
    """A partial first attempt leaves no active/readable run, summary, or variant for the source."""
    failing = StubLLMClient(responder=_fail_after_k_context(0))
    with pytest.raises(RuntimeError):
        process_document(
            engine,
            failing,
            source_id=_AIZK_UUID,
            conversion_output_id=_OUTPUT_ID,
            markdown_source=_StubMarkdownSource(_MARKDOWN),
            freshness=_AlwaysCurrent(),
        )

    assert _active_runs(engine, _SCOPE) == {}, "no run is active for the source"
    with Session(engine) as session:
        assert session.exec(select(DocumentSummary)).all() == [], "no summary is readable"
        assert session.exec(select(ContextualizedChunk)).all() == [], "no variant is readable"


def test_partial_failure_under_changed_inputs_leaves_prior_generation_unchanged(engine: Engine) -> None:
    """A failed re-contextualization under changed inputs leaves the prior completed generation intact."""
    _process(engine, StubLLMClient(responder=_responder(summary="gen-one summary")))
    before = _variant_snapshot(engine, _SCOPE)

    # Re-contextualize under changed markdown (a new conversion output): fail mid-revision.
    changed_markdown = _MARKDOWN + "\n\n## Section Four\n\nNew content that changes the chunk set entirely.\n"
    failing = StubLLMClient(responder=_fail_after_k_context(0, summary="gen-two summary"))
    with pytest.raises(RuntimeError):
        process_document(
            engine,
            failing,
            source_id=_AIZK_UUID,
            conversion_output_id=43,
            markdown_source=_StubMarkdownSource(changed_markdown),
            freshness=_AlwaysCurrent(),
        )

    assert _variant_snapshot(engine, _SCOPE) == before, "the prior completed generation is undisturbed"


def test_incremental_durability_checkpoints_each_output(engine: Engine) -> None:
    """When generation fails after K outputs, exactly those K (plus the summary) are retained."""
    chunks = [_chunk(f"chunk {i} body text", i) for i in range(4)]
    # Fail on the 3rd chunk → summary + first 2 revisions are checkpointed, the rest are not.
    failing = StubLLMClient(responder=_fail_after_k_context(2))
    summary_text = resolve_summary_text(
        engine, failing, source_id=_SCOPE, markdown_hash_xx64=_HASH, document_text=_DOC_TEXT
    )
    with pytest.raises(RuntimeError, match="model failed on context call"):
        resolve_revisions(
            engine,
            failing,
            source_id=_SCOPE,
            summary_text=summary_text,
            markdown_hash_xx64=_HASH,
            ordered_chunks=chunks,
            splitter_version=SPLITTER_VERSION,
        )

    assert len(_memo_rows(engine, _SCOPE, MEMO_KIND_SUMMARY)) == 1, "the summary is checkpointed"
    assert len(_memo_rows(engine, _SCOPE, MEMO_KIND_REVISION)) == 2, "exactly the produced revisions are checkpointed"


# --------------------------------------------------------------------------- #
# Stale-plan guards: a run planned for reuse changed before apply (defense-in-depth)
# --------------------------------------------------------------------------- #


def test_summarize_document_reuse_with_mismatched_planned_summary_is_retryable(engine: Engine) -> None:
    """Reusing an active summary whose text differs from the planned one fails retryably.

    Guards against persisting revisions conditioned on summary Y while recording a
    reused summary X (an overlapping re-summarization under the same key between the
    plan and apply phases). The mismatch raises a retryable :class:`StalePlanError`
    rather than silently recording mismatched provenance.
    """
    with Session(engine) as session:
        summarize_document(
            session,
            StubLLMClient(responder=lambda _p: "summary-X"),
            source_id=_SCOPE,
            conversion_output_id="out",
            markdown_hash_xx64=_HASH,
            document_text=_DOC_TEXT,
        )
        session.commit()

    # An overlapping attempt's revisions were planned against a different summary text.
    with Session(engine) as session, pytest.raises(StalePlanError, match="changed since the revisions were planned"):
        summarize_document(
            session,
            StubLLMClient(),
            source_id=_SCOPE,
            conversion_output_id="out",
            markdown_hash_xx64=_HASH,
            document_text=_DOC_TEXT,
            precomputed_summary_text="summary-Y",
        )


def test_contextualize_chunks_reuse_only_without_active_run_is_retryable(engine: Engine) -> None:
    """``reuse_only`` with no complete active variant run raises a retryable StalePlanError.

    The generation phase planned to reuse a complete active variant run (so it
    carries no revisions), but the run is gone at persist. Rather than misreading the
    empty set as a torn run (a permanent ValueError), it fails retryably so the unit
    regenerates outside the write lock on retry.
    """
    chunks = [_chunk("body one", 0)]
    with Session(engine) as session:
        run, persisted = persist_chunks(
            session,
            source_id=_SCOPE,
            conversion_output_id="out",
            markdown_hash_xx64=_HASH,
            splitter_version=SPLITTER_VERSION,
            chunks=chunks,
        )
        summary = summarize_document(
            session,
            StubLLMClient(),
            source_id=_SCOPE,
            conversion_output_id="out",
            markdown_hash_xx64=_HASH,
            document_text=_DOC_TEXT,
        )
        # No active variant run exists, so reuse_only cannot be satisfied.
        with pytest.raises(StalePlanError, match="planned for reuse is no longer active"):
            contextualize_chunks(
                session,
                StubLLMClient(),
                source_id=_SCOPE,
                summary=summary,
                chunks=persisted,
                chunking_run_id=run.id,
                splitter_version=SPLITTER_VERSION,
                precomputed_revisions=None,
                reuse_only=True,
            )


def test_stale_plan_error_is_classified_retryable_not_permanent() -> None:
    """The stage handler maps a StalePlanError to a retryable failure, not permanent.

    A ValueError maps to a permanent failure; StalePlanError must not, so a stale
    plan re-resolves on the next attempt instead of permanently failing the unit.
    """
    handler = ContextualizationStageHandler(None, None, None, None)  # type: ignore[arg-type]
    outcome = handler.map_result(StalePlanError("stale"))
    assert outcome.retry_class is RetryClass.RETRYABLE
    assert not isinstance(StalePlanError("stale"), ValueError), "must not be a ValueError (permanent)"


# --------------------------------------------------------------------------- #
# Small helpers that need module-internal derivation keys
# --------------------------------------------------------------------------- #


def memo_get_via(engine: Engine, kind: str, scope: str, derivation_key: str) -> str | None:
    """Read a memo value through a short-lived session."""
    with Session(engine) as session:
        return memo_get(session, kind, scope, derivation_key)


def _revision_key(chunk: SplitterChunk) -> str:
    """Compute the per-row revision derivation key for a single-chunk document."""
    from aizk.graph._version import SUMMARY_VERSION
    from aizk.graph.contextualization import (
        _summary_derivation_key,
        _summary_identity_from_text,
        _variant_row_derivation_key,
    )

    summary_derivation_key = _summary_derivation_key(_HASH, SUMMARY_VERSION, "default")
    summary_identity = _summary_identity_from_text("summary", SUMMARY_VERSION, summary_derivation_key)
    from aizk.graph._version import CONTEXT_VERSION

    return _variant_row_derivation_key(
        summary_identity, chunk, None, None, None, SPLITTER_VERSION, CONTEXT_VERSION, "default"
    )


def _all_runs(engine: Engine, scope: str) -> Iterator[PipelineRun]:
    """Yield all runs (any status) for a source."""
    with Session(engine) as session:
        yield from session.exec(select(PipelineRun).where(PipelineRun.scope_id == scope)).all()


# Helpers used above for memo reads on the summary key need the summary derivation key.
def _summary_key() -> str:
    """Compute the summary derivation key used by the resolve-level tests."""
    from aizk.graph._version import SUMMARY_VERSION
    from aizk.graph.contextualization import _summary_derivation_key

    return _summary_derivation_key(_HASH, SUMMARY_VERSION, "default")
