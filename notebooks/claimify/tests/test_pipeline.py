"""Orchestrator and JSONL-roundtrip tests (pure functions, stubbed runners)."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import uuid4

from _claimify import io as claimify_io
from _claimify.models import (
    AtomicClaim,
    ClaimRecord,
    DecompositionResult,
    DisambigResult,
    ExtractedClaim,
    ExtractionRecord,
    FailedExtraction,
    FailedRecord,
    LoadedDoc,
    Section,
    SelectionResult,
    SentenceContext,
)
from _claimify.pipeline import build_sentence_contexts, extract_claims
import pytest


@pytest.fixture(autouse=True)
def _stub_nltk(monkeypatch):
    """Make tests hermetic: skip NLTK data download and use a simple tokenizer."""
    monkeypatch.setattr("_claimify.pipeline.ensure_punkt_tab", lambda: None)
    import _claimify.pipeline as pipeline_module
    import nltk.tokenize as nltk_tokenize

    def _simple_sent_tokenize(text: str) -> list[str]:
        import re

        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]

    monkeypatch.setattr(nltk_tokenize, "sent_tokenize", _simple_sent_tokenize)


class _StubContextAgent:
    """Stand-in for a pydantic-ai Agent; pipeline only calls .run via contextualize_section."""


async def _fake_contextualize_section(agent, doc, section):
    return f"ctx-for-{'.'.join(section.heading_path) or 'lead'}"


def _doc(md: str) -> LoadedDoc:
    return LoadedDoc(
        aizk_uuid=uuid4(),
        karakeep_id="kk-test",
        title="Test Doc",
        markdown=md,
        source="cache",
    )


def _make_runners(
    *,
    selection: SelectionResult,
    disambig: DisambigResult,
    decomposition: DecompositionResult,
):
    async def selection_runner(ctx: SentenceContext, question: str) -> SelectionResult:
        return selection

    async def disambig_runner(ctx: SentenceContext, question: str) -> DisambigResult:
        return disambig

    async def decomp_runner(ctx: SentenceContext, question: str) -> DecompositionResult:
        return decomposition

    return selection_runner, disambig_runner, decomp_runner


def test_build_sentence_contexts_windows():
    section = Section(
        heading_path=("A",),
        content="Alpha sentence. Beta sentence. Gamma sentence. Delta sentence.",
        start_index=0,
        end_index=60,
    )
    contexts = build_sentence_contexts(section, section_idx=2, p=1, f=1)
    assert len(contexts) == 4
    assert contexts[0].preceding == ""
    assert contexts[0].following.startswith("Beta")
    assert contexts[2].preceding.startswith("Beta")
    assert contexts[2].following.startswith("Delta")
    assert contexts[0].section_idx == 2
    assert contexts[-1].sentence_idx == 3


def test_extract_claims_full_path_produces_claim_records(monkeypatch):
    monkeypatch.setattr("_claimify.pipeline.contextualize_section", _fake_contextualize_section)

    doc = _doc("# Top\nAlpha sentence. Beta sentence.\n")

    selection = SelectionResult(contains_proposition=True, rewritten_sentence="Alpha rewritten.", reasoning="r")
    disambig = DisambigResult(
        can_be_disambiguated=True, decontextualized_sentence="Alpha decontextualized.", reasoning="r"
    )
    decomposition = DecompositionResult(claims=[AtomicClaim(proposition="Alpha is a thing.", essential_context=None)])
    sel_r, dis_r, dec_r = _make_runners(selection=selection, disambig=disambig, decomposition=decomposition)

    records = asyncio.run(
        extract_claims(
            doc,
            context_agent=_StubContextAgent(),
            selection=sel_r,
            disambiguation=dis_r,
            decomposition=dec_r,
            p=1,
            f=1,
        )
    )
    assert all(isinstance(r, ClaimRecord) for r in records)
    assert len(records) == 2  # two sentences, one claim each
    claim_rec = cast(ClaimRecord, records[0])
    assert claim_rec.claim.heading_path == ["Top"]
    assert claim_rec.claim.context_str.startswith("ctx-for-Top")
    # The final form handed to decomposition (and persisted) is the
    # decontextualized sentence — verifies chaining, not a fixed string.
    assert claim_rec.claim.sentence == "Alpha decontextualized."


def test_extract_claims_chains_rewritten_and_decontextualized_sentences(monkeypatch):
    """Selection.rewritten_sentence feeds disambiguation; disambig output feeds decomposition."""
    monkeypatch.setattr("_claimify.pipeline.contextualize_section", _fake_contextualize_section)
    doc = _doc("# Top\nAlpha sentence.\n")

    seen: dict[str, str] = {}

    async def sel_r(ctx, question):
        seen["selection"] = ctx.sentence
        return SelectionResult(contains_proposition=True, rewritten_sentence="Alpha rewritten.", reasoning="")

    async def dis_r(ctx, question):
        seen["disambiguation"] = ctx.sentence
        return DisambigResult(
            can_be_disambiguated=True, decontextualized_sentence="Alpha decontextualized.", reasoning=""
        )

    async def dec_r(ctx, question):
        seen["decomposition"] = ctx.sentence
        return DecompositionResult(claims=[AtomicClaim(proposition="Alpha is a thing.", essential_context=None)])

    asyncio.run(
        extract_claims(
            doc,
            context_agent=_StubContextAgent(),
            selection=sel_r,
            disambiguation=dis_r,
            decomposition=dec_r,
            p=1,
            f=1,
        )
    )
    assert seen["selection"] == "Alpha sentence."
    assert seen["disambiguation"] == "Alpha rewritten."
    assert seen["decomposition"] == "Alpha decontextualized."


def test_extract_claims_question_receives_context_str(monkeypatch):
    """`question_for` runs after contextualization and receives context_str."""
    monkeypatch.setattr("_claimify.pipeline.contextualize_section", _fake_contextualize_section)
    doc = _doc("# Top\nAlpha sentence.\n")

    captured: dict[str, str] = {}

    def q_for(doc_, section_, context_str_):
        captured["context_str"] = context_str_
        return f"Q[{context_str_}]"

    async def sel_r(ctx, question):
        captured["question"] = question
        return SelectionResult(contains_proposition=False, rewritten_sentence=None, reasoning="")

    async def _boom(ctx, question):
        raise AssertionError("should not run")

    asyncio.run(
        extract_claims(
            doc,
            context_agent=_StubContextAgent(),
            selection=sel_r,
            disambiguation=_boom,
            decomposition=_boom,
            p=1,
            f=1,
            question_for=q_for,
        )
    )
    assert captured["context_str"] == "ctx-for-Top"
    assert captured["question"] == "Q[ctx-for-Top]"


def test_extract_claims_skips_when_selection_false(monkeypatch):
    monkeypatch.setattr("_claimify.pipeline.contextualize_section", _fake_contextualize_section)
    doc = _doc("# Top\nAlpha sentence.\n")

    selection = SelectionResult(contains_proposition=False, rewritten_sentence=None, reasoning="r")

    # disambig/decomp runners must not be called; return stubs that raise if invoked
    async def _boom(ctx, question):
        raise AssertionError("disambiguation/decomposition should not run")

    sel_r, _, _ = _make_runners(
        selection=selection,
        disambig=DisambigResult(can_be_disambiguated=True, decontextualized_sentence="x", reasoning=""),
        decomposition=DecompositionResult(claims=[]),
    )

    records = asyncio.run(
        extract_claims(
            doc,
            context_agent=_StubContextAgent(),
            selection=sel_r,
            disambiguation=_boom,
            decomposition=_boom,
            p=1,
            f=1,
        )
    )
    assert records == []


def test_extract_claims_emits_failed_record_on_stage_exception(monkeypatch):
    monkeypatch.setattr("_claimify.pipeline.contextualize_section", _fake_contextualize_section)
    doc = _doc("# Top\nAlpha sentence.\n")

    async def sel_r(ctx, question):
        return SelectionResult(contains_proposition=True, rewritten_sentence="a", reasoning="")

    async def dis_r(ctx, question):
        raise RuntimeError("boom")

    async def dec_r(ctx, question):
        raise AssertionError("should not run after disambiguation fails")

    records = asyncio.run(
        extract_claims(
            doc,
            context_agent=_StubContextAgent(),
            selection=sel_r,
            disambiguation=dis_r,
            decomposition=dec_r,
            p=1,
            f=1,
        )
    )
    assert len(records) == 1
    failed = cast(FailedRecord, records[0])
    assert isinstance(failed, FailedRecord)
    assert failed.failure.stage == "disambiguation"
    assert "boom" in failed.failure.error


def test_extraction_jsonl_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(claimify_io, "EXTRACTION_DIR", tmp_path)

    doc_uuid = uuid4()
    records: list[ExtractionRecord] = [
        ClaimRecord(
            claim=ExtractedClaim(
                doc_uuid=doc_uuid,
                heading_path=["A", "B"],
                section_idx=1,
                sentence_idx=0,
                sentence="A sentence.",
                claim=AtomicClaim(proposition="A is a thing.", essential_context="ctx"),
                context_str="ctx",
            )
        ),
        FailedRecord(
            failure=FailedExtraction(
                doc_uuid=doc_uuid,
                section_idx=2,
                sentence_idx=3,
                stage="selection",
                error="RuntimeError: x",
            )
        ),
    ]
    written = claimify_io.write_extraction_jsonl(doc_uuid, records)
    assert written == tmp_path / f"{doc_uuid}.jsonl"

    loaded = claimify_io.read_extraction_jsonl(doc_uuid)
    assert [r.kind for r in loaded] == ["claim", "failed"]
    assert cast(ClaimRecord, loaded[0]).claim.sentence == "A sentence."
    assert cast(FailedRecord, loaded[1]).failure.stage == "selection"
