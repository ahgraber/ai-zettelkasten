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
