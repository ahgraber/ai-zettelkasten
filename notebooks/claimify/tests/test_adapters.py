"""Tests for Path A prose parsers (pure-function, no LLM)."""

from __future__ import annotations

from textwrap import dedent

from _claimify.adapters import (
    AdapterParseError,
    parse_decomposition,
    parse_disambiguation,
    parse_selection,
    with_schema_suffix,
)
from _claimify.models import SelectionResult
import pytest

# ---------- Selection ----------


def test_parse_selection_contains_with_rewrite():
    raw = dedent(
        """\
        Sentence:
        The partnership illustrates innovation.

        <4-step reasoning omitted>

        Final submission:
        Contains a specific and verifiable proposition
        Sentence with only verifiable information:
        There is a partnership between Company X and Company Y
        """
    )
    result = parse_selection(raw, sentence="The partnership illustrates innovation.")
    assert result.contains_proposition is True
    assert result.rewritten_sentence == "There is a partnership between Company X and Company Y"


def test_parse_selection_remains_unchanged_echoes_input():
    raw = dedent(
        """\
        Final submission:
        Contains a specific and verifiable proposition
        Sentence with only verifiable information:
        remains unchanged
        """
    )
    sentence = "Jane emphasizes the importance of collaboration."
    result = parse_selection(raw, sentence=sentence)
    assert result.contains_proposition is True
    assert result.rewritten_sentence == sentence


def test_parse_selection_does_not_contain_yields_none():
    raw = dedent(
        """\
        Final submission:
        Does NOT contain a specific and verifiable proposition
        Sentence with only verifiable information:
        None
        """
    )
    result = parse_selection(raw, sentence="Technology should be inclusive.")
    assert result.contains_proposition is False
    assert result.rewritten_sentence is None


def test_parse_selection_missing_markers_raises():
    with pytest.raises(AdapterParseError, match="markers"):
        parse_selection("garbled output with no markers", sentence="x")


# ---------- Disambiguation ----------


def test_parse_disambiguation_decontextualized():
    raw = dedent(
        """\
        Incomplete Names, Acronyms, Abbreviations:
        CGP -> Committee for Global Peace

        Linguistic Ambiguity in 'The CGP has called for the termination of hostilities':
        Referential: "hostilities" resolved by context.

        Changes Needed to Decontextualize the Sentence:
        Expand CGP; add 'in the Middle East' qualifier.

        DecontextualizedSentence: The Committee for Global Peace has called for the termination of hostilities in the Middle East.
        """
    )
    result = parse_disambiguation(raw)
    assert result.can_be_disambiguated is True
    assert result.decontextualized_sentence == (
        "The Committee for Global Peace has called for the termination of hostilities in the Middle East."
    )


def test_parse_disambiguation_cannot_be_decontextualized():
    raw = dedent(
        """\
        Linguistic Ambiguity in 'Sentence':
        Structural ambiguity cannot be resolved.

        DecontextualizedSentence: Cannot be decontextualized
        """
    )
    result = parse_disambiguation(raw)
    assert result.can_be_disambiguated is False
    assert result.decontextualized_sentence is None


def test_parse_disambiguation_missing_marker_raises():
    with pytest.raises(AdapterParseError, match="DecontextualizedSentence"):
        parse_disambiguation("some reasoning without the marker")


# ---------- Decomposition ----------


def test_parse_decomposition_with_clarifications():
    raw = dedent(
        """\
        Sentence: The local council expects its law to pass in January 2025.
        MaxClarifiedSentence: The Boston local council expects its law banning plastic bags to pass in January 2025.
        The range of the possible number of propositions (with some margin for variation) is: 1-2
        Specific, Verifiable, and Decontextualized Propositions:
        [
        "The Boston local council expects its law banning plastic bags to pass in January 2025",
        ]
        Specific, Verifiable, and Decontextualized Propositions with Essential Context/Clarifications:
        [
        "The [Boston] local council expects its law [banning plastic bags] to pass in January 2025 - true or false?",
        ]
        """
    )
    result = parse_decomposition(raw)
    assert len(result.claims) == 1
    claim = result.claims[0]
    assert "The [Boston] local council" in claim.proposition
    assert claim.proposition.endswith("to pass in January 2025")
    assert claim.essential_context == "Boston; banning plastic bags"


def test_parse_decomposition_multiple_propositions():
    raw = dedent(
        """\
        Specific, Verifiable, and Decontextualized Propositions with Essential Context/Clarifications:
        [
        "John Smith led TurboCorp's operations team [in 2010] - true or false?",
        "John Smith led TurboCorp's finance team [in 2010] - true or false?",
        ]
        """
    )
    result = parse_decomposition(raw)
    assert len(result.claims) == 2
    assert all(c.essential_context == "in 2010" for c in result.claims)


def test_parse_decomposition_missing_marker_raises():
    with pytest.raises(AdapterParseError, match="missing marker"):
        parse_decomposition("no recognizable decomposition output")


# ---------- Path B schema suffix ----------


def test_with_schema_suffix_appends_json_schema():
    suffix_prompt = with_schema_suffix("Original prompt body.", SelectionResult)
    assert "Original prompt body." in suffix_prompt
    assert "---JSON---" in suffix_prompt
    assert '"contains_proposition"' in suffix_prompt
