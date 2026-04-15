"""Eval-adapter parser tests + bundle smoke test (pure-function, no LLM)."""

from __future__ import annotations

from textwrap import dedent

from _claimify.adapters import (
    AdapterParseError,
    parse_coverage,
    parse_decontextualization,
    parse_element,
    parse_entailment,
    parse_invalid_claim,
    parse_invalid_sentence,
)
import pytest

# ---------- invalid_sentences ----------


def test_parse_invalid_sentence_positive():
    raw = dedent(
        """\
        S = Some examples include:
        Describe the context for S. Header for a list.
        S cannot be interpreted as a complete, declarative sentence
        """
    )
    result = parse_invalid_sentence(raw)
    assert result.is_invalid is True


def test_parse_invalid_sentence_negative():
    raw = dedent(
        """\
        S = - Sourcing materials from sustainable suppliers
        ...
        S can be interpreted as a complete, declarative sentence
        """
    )
    result = parse_invalid_sentence(raw)
    assert result.is_invalid is False


def test_parse_invalid_sentence_missing_raises():
    with pytest.raises(AdapterParseError, match="can/cannot"):
        parse_invalid_sentence("random words with no verdict")


# ---------- element ----------


def test_parse_element_extracts_list():
    raw = dedent(
        """\
        S = Jane Smith is a notable sustainability leader.
        What are ALL elements of S_restated?
        [
          "Jane Smith is a notable sustainability leader -> contains verifiable information",
          "CleanTech is Jane Smith's organization -> contains verifiable information",
        ]
        """
    )
    result = parse_element(raw)
    assert len(result.elements) == 2
    assert "Jane Smith is a notable sustainability leader" in result.elements[0]
    assert "contains verifiable information" in result.elements[0]


def test_parse_element_missing_marker_raises():
    with pytest.raises(AdapterParseError, match="What are ALL elements"):
        parse_element("no element list here")


# ---------- coverage ----------


def test_parse_coverage_all_verdicts():
    raw = dedent(
        """\
        E1: The sky is blue
        - some reasoning
        - Therefore E1 is fully covered by C
        E2: Grass is green
        - some reasoning
        - Therefore E2 is not fully covered by C
        E3: Water is wet
        - Therefore E3 is fully covered by C
        """
    )
    result = parse_coverage(raw, n_elements=3)
    assert result.per_element_covered == [True, False, True]


def test_parse_coverage_count_mismatch_raises():
    raw = "E1 is fully covered by C"
    with pytest.raises(AdapterParseError, match="expected 3"):
        parse_coverage(raw, n_elements=3)


# ---------- entailment ----------


def test_parse_entailment_entails():
    raw = dedent(
        """\
        S = Alice discovered the oil spill.
        ...reasoning...
        Therefore, S entails all elements of C.
        """
    )
    result = parse_entailment(raw)
    assert result.entailed is True


def test_parse_entailment_does_not_entail():
    raw = dedent(
        """\
        S = Alice discussed the oil spill.
        ...reasoning...
        Therefore, S does not entail all elements of C.
        """
    )
    result = parse_entailment(raw)
    assert result.entailed is False


def test_parse_entailment_missing_raises():
    with pytest.raises(AdapterParseError, match="entail"):
        parse_entailment("no verdict marker")


# ---------- decontextualization ----------


def test_parse_decontextualization_rewritten():
    raw = dedent(
        """\
        C = The court's decision affected abortion laws across the United States.
        Would someone reading C without any context have questions? Yes.
        C_max = The Supreme Court's decision in Roe v. Wade in January 1973 affected abortion laws across the United States.
        """
    )
    result = parse_decontextualization(
        raw, claim="The court's decision affected abortion laws across the United States."
    )
    assert "Supreme Court" in result.c_max_text
    assert "Roe v. Wade" in result.c_max_text


def test_parse_decontextualization_cmax_is_c_echoes_claim():
    raw = dedent(
        """\
        C = Grass is green.
        Would someone reading C without any context have questions? No.
        C_max = C
        """
    )
    claim = "Grass is green."
    result = parse_decontextualization(raw, claim=claim)
    assert result.c_max_text == claim


def test_parse_decontextualization_missing_raises():
    with pytest.raises(AdapterParseError, match="C_max"):
        parse_decontextualization("no c_max line", claim="x")


# ---------- invalid_claims ----------


def test_parse_invalid_claim_not_complete():
    raw = dedent(
        """\
        C = Sourcing materials from sustainable suppliers
        In isolation, is C a complete, declarative sentence? It's missing a subject and a verb, so C is not a complete, declarative sentence.
        """
    )
    result = parse_invalid_claim(raw)
    assert result.is_invalid is True


def test_parse_invalid_claim_complete():
    raw = dedent(
        """\
        C = Grass is green.
        In isolation, is C a complete, declarative sentence? Yes, C is a complete, declarative sentence.
        """
    )
    result = parse_invalid_claim(raw)
    assert result.is_invalid is False


def test_parse_invalid_claim_missing_raises():
    with pytest.raises(AdapterParseError, match="is/is-not"):
        parse_invalid_claim("unparsable text")


# ---------- bundle smoke ----------


from uuid import uuid4

from _claimify.evaluation import (
    ALL_DIMENSIONS,
    agreement_table,
    baseline_majority,
    cohens_kappa,
)
from _claimify.models import EvalRecord

# ---------- baseline_majority ----------


def test_baseline_majority_bools():
    assert baseline_majority({"m1": True, "m2": True, "m3": False}) is True


def test_baseline_majority_list_of_bools_elementwise():
    verdicts = {
        "m1": [True, False, True],
        "m2": [True, True, True],
        "m3": [False, False, True],
    }
    assert baseline_majority(verdicts) == [True, False, True]


def test_baseline_majority_strings_normalized():
    verdicts = {
        "m1": "The cat sat on the mat.",
        "m2": "the cat sat on the mat",
        "m3": "A dog barked.",
    }
    assert baseline_majority(verdicts) in {"The cat sat on the mat.", "the cat sat on the mat"}


def test_baseline_majority_empty_returns_none():
    assert baseline_majority({}) is None


# ---------- cohens_kappa ----------


def test_cohens_kappa_perfect_agreement():
    assert cohens_kappa([True, False, True], [True, False, True]) == pytest.approx(1.0)


def test_cohens_kappa_chance_level_near_zero():
    a = [True, False] * 20
    b = [False, True] * 20
    assert cohens_kappa(a, b) == pytest.approx(-1.0)


def test_cohens_kappa_length_mismatch_raises():
    with pytest.raises(ValueError, match="length mismatch"):
        cohens_kappa([True], [True, False])


# ---------- agreement_table ----------


def _vrec(
    model: str,
    dimension: str,
    result_json: dict,
    *,
    claim_idx: int | None = None,
    sentence_idx: int = 0,
    section_idx: int = 0,
) -> EvalRecord:
    return EvalRecord(
        doc_uuid=uuid4(),
        section_idx=section_idx,
        sentence_idx=sentence_idx,
        claim_idx=claim_idx,
        dimension=dimension,
        model=model,
        result_json=result_json,
        raw=None,
    )


def test_agreement_table_uses_baseline_tier_majority_with_kappa():
    """Baseline = majority of models in tiers[baseline_tier]; bool dim metric is Cohen's κ."""
    # Two units; baseline tier is {b1, b2}. Their majority is True on both.
    # Non-baseline tier has m1 (both True) and m2 (both False).
    verdicts = [
        # sentence_idx=0: all four agree True
        _vrec("b1", "invalid_sentence", {"is_invalid": True}, sentence_idx=0),
        _vrec("b2", "invalid_sentence", {"is_invalid": True}, sentence_idx=0),
        _vrec("m1", "invalid_sentence", {"is_invalid": True}, sentence_idx=0),
        _vrec("m2", "invalid_sentence", {"is_invalid": False}, sentence_idx=0),
        # sentence_idx=1: baseline True; m1 True, m2 False
        _vrec("b1", "invalid_sentence", {"is_invalid": True}, sentence_idx=1),
        _vrec("b2", "invalid_sentence", {"is_invalid": True}, sentence_idx=1),
        _vrec("m1", "invalid_sentence", {"is_invalid": True}, sentence_idx=1),
        _vrec("m2", "invalid_sentence", {"is_invalid": False}, sentence_idx=1),
    ]
    tiers = {"baseline": ["b1", "b2"], "other": ["m1", "m2"]}
    df = agreement_table(verdicts, tiers)
    # m1 agrees with baseline on both units: κ is well-defined only with label
    # variance, but our fallback returns 1.0 on perfect match — accept either.
    assert df.loc["m1", "invalid_sentence"] == pytest.approx(1.0)
    # m2 disagrees on both units; κ is -1 or 0 depending on label distribution.
    assert df.loc["m2", "invalid_sentence"] <= 0.0


def test_agreement_table_rejects_unknown_baseline_tier():
    verdicts = [_vrec("m1", "invalid_sentence", {"is_invalid": True})]
    with pytest.raises(ValueError, match="baseline_tier"):
        agreement_table(verdicts, {"tier": ["m1"]})


def test_agreement_table_scores_element_via_jaccard():
    # baseline majority (b1,b2 same) = ["alpha", "beta"] normalized
    verdicts = [
        _vrec("b1", "element", {"elements": ["alpha", "beta"]}),
        _vrec("b2", "element", {"elements": ["alpha", "beta"]}),
        _vrec("m1", "element", {"elements": ["alpha", "beta"]}),  # perfect overlap
        _vrec("m2", "element", {"elements": ["alpha"]}),  # half overlap (1/2)
    ]
    df = agreement_table(verdicts, {"baseline": ["b1", "b2"], "other": ["m1", "m2"]})
    assert df.loc["m1", "element"] == pytest.approx(1.0)
    assert df.loc["m2", "element"] == pytest.approx(0.5)


def test_agreement_table_scores_decontextualization_exact_and_normalized():
    verdicts = [
        _vrec("b1", "decontextualization", {"c_max_text": "The cat sat."}, claim_idx=0),
        _vrec("b2", "decontextualization", {"c_max_text": "The cat sat."}, claim_idx=0),
        # m1 exact match
        _vrec("m1", "decontextualization", {"c_max_text": "The cat sat."}, claim_idx=0),
        # m2 normalized-only match (trailing punct + case)
        _vrec("m2", "decontextualization", {"c_max_text": "the cat sat"}, claim_idx=0),
    ]
    df = agreement_table(verdicts, {"baseline": ["b1", "b2"], "other": ["m1", "m2"]})
    assert df.loc["m1", "decontextualization_exact"] == pytest.approx(1.0)
    assert df.loc["m1", "decontextualization_normalized"] == pytest.approx(1.0)
    assert df.loc["m2", "decontextualization_exact"] == pytest.approx(0.0)
    assert df.loc["m2", "decontextualization_normalized"] == pytest.approx(1.0)


def test_all_dimensions_covers_expected_set():
    assert set(ALL_DIMENSIONS) == {
        "invalid_sentence",
        "element",
        "coverage",
        "entailment",
        "decontextualization",
        "invalid_claim",
    }


# ---------- eval JSONL roundtrip ----------


def test_evaluation_jsonl_roundtrip(tmp_path, monkeypatch):
    from _claimify import io as claimify_io

    monkeypatch.setattr(claimify_io, "EVALUATION_DIR", tmp_path)

    doc_uuid = uuid4()
    records = [
        EvalRecord(
            doc_uuid=doc_uuid,
            section_idx=1,
            sentence_idx=0,
            claim_idx=None,
            dimension="invalid_sentence",
            model="openai/gpt-5-mini",
            result_json={"is_invalid": False, "reasoning": "ok"},
            raw=None,
        ),
        EvalRecord(
            doc_uuid=doc_uuid,
            section_idx=1,
            sentence_idx=0,
            claim_idx=0,
            dimension="entailment",
            model="openai/gpt-5-mini",
            result_json={"entailed": True, "reasoning": "ok"},
            raw=None,
        ),
    ]
    path = claimify_io.write_evaluation_jsonl(doc_uuid, records)
    loaded = claimify_io.read_evaluation_jsonl(doc_uuid)
    assert path == tmp_path / f"{doc_uuid}.jsonl"
    assert [r.dimension for r in loaded] == ["invalid_sentence", "entailment"]
    assert loaded[1].claim_idx == 0


def test_evaluate_claims_with_stubbed_bundle(tmp_path, monkeypatch):
    """Orchestrator wires per-dimension calls without touching the network."""
    import asyncio

    from _claimify import evaluation as ev, io as claimify_io

    # Hermetic: skip NLTK data download + use a tiny regex tokenizer.
    monkeypatch.setattr("_claimify.pipeline.ensure_punkt_tab", lambda: None)
    import nltk.tokenize as nltk_tokenize

    def _simple_sent_tokenize(text: str) -> list[str]:
        import re

        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]

    monkeypatch.setattr(nltk_tokenize, "sent_tokenize", _simple_sent_tokenize)
    from _claimify.models import (
        AtomicClaim,
        ClaimRecord,
        CoverageResult,
        DecontextResult,
        ElementResult,
        EntailmentResult,
        ExtractedClaim,
        InvalidClaimVerdict,
        InvalidSentenceVerdict,
        LoadedDoc,
    )

    monkeypatch.setattr(claimify_io, "EVALUATION_DIR", tmp_path)

    doc_uuid = uuid4()
    doc = LoadedDoc(
        aizk_uuid=doc_uuid,
        karakeep_id="kk-x",
        title="Doc X",
        markdown="# Top\nAlpha sentence. Beta sentence.\n",
        source="cache",
    )
    claim_record = ClaimRecord(
        claim=ExtractedClaim(
            doc_uuid=doc_uuid,
            heading_path=["Top"],
            section_idx=0,
            sentence_idx=0,
            sentence="Alpha sentence.",
            claim=AtomicClaim(proposition="Alpha is a thing.", essential_context=None),
            context_str="ctx",
        )
    )

    async def _inv_sent(q, e, s):
        return InvalidSentenceVerdict(is_invalid=False, reasoning="")

    async def _elem(q, e, s):
        return ElementResult(elements=["Alpha is a thing -> contains verifiable information"])

    async def _cov(q, e, claims, elements):
        return CoverageResult(per_element_covered=[True])

    async def _ent(q, e, s, c):
        return EntailmentResult(entailed=True, reasoning="")

    async def _dec(q, e, s, all_c, c):
        return DecontextResult(c_max_text=c, reasoning="")

    async def _inv_claim(c):
        return InvalidClaimVerdict(is_invalid=False, reasoning="")

    stub_bundle = ev.EvalAgentBundle(
        model="stub/model",
        invalid_sentence=_inv_sent,
        element=_elem,
        coverage=_cov,
        entailment=_ent,
        decontextualization=_dec,
        invalid_claim=_inv_claim,
    )

    monkeypatch.setattr(ev, "bundle_for", lambda m, **kw: stub_bundle)

    path = asyncio.run(
        ev.evaluate_claims(
            doc,
            [claim_record],
            tiers={"baseline": ["stub/model"]},
        )
    )
    loaded = claimify_io.read_evaluation_jsonl(doc_uuid)
    dims = {r.dimension for r in loaded}
    assert dims == set(ev.ALL_DIMENSIONS)
    assert path == tmp_path / f"{doc_uuid}.jsonl"


def test_evaluate_claims_runs_invalid_sentence_on_zero_claim_sentences(tmp_path, monkeypatch):
    """invalid_sentence must fire on sentences Selection dropped (no ClaimRecord)."""
    import asyncio

    from _claimify import evaluation as ev, io as claimify_io
    from _claimify.models import (
        AtomicClaim,
        ClaimRecord,
        CoverageResult,
        DecontextResult,
        ElementResult,
        EntailmentResult,
        ExtractedClaim,
        InvalidClaimVerdict,
        InvalidSentenceVerdict,
        LoadedDoc,
    )

    monkeypatch.setattr(claimify_io, "EVALUATION_DIR", tmp_path)
    monkeypatch.setattr("_claimify.pipeline.ensure_punkt_tab", lambda: None)
    import nltk.tokenize as nltk_tokenize

    def _simple_sent_tokenize(text: str) -> list[str]:
        import re

        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]

    monkeypatch.setattr(nltk_tokenize, "sent_tokenize", _simple_sent_tokenize)

    doc_uuid = uuid4()
    doc = LoadedDoc(
        aizk_uuid=doc_uuid,
        karakeep_id="kk-x",
        title="Doc X",
        # Three sentences; only sentence_idx=1 produces a claim.
        markdown="# Top\nAlpha sentence. Beta sentence. Gamma sentence.\n",
        source="cache",
    )
    claim_record = ClaimRecord(
        claim=ExtractedClaim(
            doc_uuid=doc_uuid,
            heading_path=["Top"],
            section_idx=0,
            sentence_idx=1,
            sentence="Beta sentence.",
            claim=AtomicClaim(proposition="Beta is a thing.", essential_context=None),
            context_str="ctx",
        )
    )

    invalid_sentence_targets: list[str] = []
    element_targets: list[str] = []

    async def _inv_sent(q, e, s):
        invalid_sentence_targets.append(s)
        return InvalidSentenceVerdict(is_invalid=False, reasoning="")

    async def _elem(q, e, s):
        element_targets.append(s)
        return ElementResult(elements=["Beta is a thing -> contains verifiable information"])

    async def _cov(q, e, claims, elements):
        return CoverageResult(per_element_covered=[True])

    async def _ent(q, e, s, c):
        return EntailmentResult(entailed=True, reasoning="")

    async def _dec(q, e, s, all_c, c):
        return DecontextResult(c_max_text=c, reasoning="")

    async def _inv_claim(c):
        return InvalidClaimVerdict(is_invalid=False, reasoning="")

    stub_bundle = ev.EvalAgentBundle(
        model="stub/model",
        invalid_sentence=_inv_sent,
        element=_elem,
        coverage=_cov,
        entailment=_ent,
        decontextualization=_dec,
        invalid_claim=_inv_claim,
    )
    monkeypatch.setattr(ev, "bundle_for", lambda m, **kw: stub_bundle)

    asyncio.run(
        ev.evaluate_claims(
            doc,
            [claim_record],
            tiers={"baseline": ["stub/model"]},
        )
    )
    assert sorted(invalid_sentence_targets) == ["Alpha sentence.", "Beta sentence.", "Gamma sentence."]
    # element fires only where claims exist (sentence_idx=1 only).
    assert element_targets == ["Beta sentence."]


def test_bundle_module_exports_expected_api():
    """Smoke: evaluation module exposes the six factories and the bundle type."""
    from _claimify import evaluation as ev

    for name in (
        "make_invalid_sentence_agent",
        "make_element_agent",
        "make_coverage_agent",
        "make_entailment_agent",
        "make_decontextualization_agent",
        "make_invalid_claim_agent",
        "bundle_for",
        "EvalAgentBundle",
    ):
        assert hasattr(ev, name), f"evaluation module missing {name}"

    fields = {f.name for f in ev.EvalAgentBundle.__dataclass_fields__.values()}
    assert fields == {
        "model",
        "invalid_sentence",
        "element",
        "coverage",
        "entailment",
        "decontextualization",
        "invalid_claim",
    }
