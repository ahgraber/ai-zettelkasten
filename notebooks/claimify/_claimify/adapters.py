"""Prompt-output adapters: prose parsers + structured-output helper.

The extraction prompts in `aizk.ai.claimify.prompts.extraction.*` ship as
labeled prose. The "prose" path parses that text into the typed result
models; the "structured" path bolts a JSON-schema tail onto the prompt and
lets pydantic-ai's `output_type=BaseModel` do the parsing.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel

from _claimify.models import (
    AtomicClaim,
    CoverageResult,
    DecompositionResult,
    DecontextResult,
    DisambigResult,
    ElementResult,
    EntailmentResult,
    InvalidClaimVerdict,
    InvalidSentenceVerdict,
    SelectionResult,
)


class AdapterParseError(ValueError):
    """Raised when an LLM output can't be coerced into the expected result model."""


def render_template(template: str, **values: str) -> str:
    """Handlebars-rendered `{{name}}` substitution for the Claimify USER_TEMPLATE files.

    Uses pydantic-handlebars — the same engine pydantic-ai's own `TemplateStr`
    uses for system-prompt templating — for syntactic parity across the app.
    """
    import pydantic_handlebars as ph

    return ph.render(template, dict(values))


_SELECTION_FINAL_RE = re.compile(r"^Final submission:\s*(.+?)\s*$", re.MULTILINE)
_SELECTION_REWRITE_RE = re.compile(
    r"^Sentence with only verifiable information:\s*(.+?)\s*$",
    re.MULTILINE,
)


def parse_selection(raw: str, *, sentence: str) -> SelectionResult:
    """Parse Selection prompt output (labeled prose) into `SelectionResult`.

    The prompt emits 'Final submission: <label>' and 'Sentence with only
    verifiable information: <text|remains unchanged|None>'. `remains unchanged`
    echoes the input sentence; `None` becomes `rewritten_sentence=None`.
    """
    final_m = _SELECTION_FINAL_RE.search(raw)
    rewrite_m = _SELECTION_REWRITE_RE.search(raw)
    if final_m is None or rewrite_m is None:
        raise AdapterParseError(
            "Selection output missing 'Final submission:' or 'Sentence with only verifiable information:' markers"
        )

    final = final_m.group(1).strip().lower()
    rewrite = rewrite_m.group(1).strip()

    if final.startswith("contains"):
        contains = True
    elif final.startswith("does not"):
        contains = False
    else:
        raise AdapterParseError(f"Selection final label unparsable: {final_m.group(1)!r}")

    if not contains or rewrite == "None":
        rewritten: str | None = None
    elif rewrite.lower() == "remains unchanged":
        rewritten = sentence
    else:
        rewritten = rewrite

    return SelectionResult(
        contains_proposition=contains,
        rewritten_sentence=rewritten,
        reasoning=raw.strip(),
    )


_DISAMB_RE = re.compile(
    r"^DecontextualizedSentence:\s*(.+?)(?=^\S[^\n]*:|\Z)",
    re.MULTILINE | re.DOTALL,
)


def parse_disambiguation(raw: str) -> DisambigResult:
    """Parse Disambiguation prompt output into `DisambigResult`.

    Prompt emits 'DecontextualizedSentence: <text>' or
    'DecontextualizedSentence: Cannot be decontextualized'. Uses the LAST
    occurrence (prompt mentions the marker inline in its instructions).
    """
    matches = list(_DISAMB_RE.finditer(raw))
    if not matches:
        # Fall back: single-line match (no trailing section header)
        m = re.search(r"^DecontextualizedSentence:\s*(.+?)\s*$", raw, re.MULTILINE)
        if m is None:
            raise AdapterParseError("Disambiguation output missing 'DecontextualizedSentence:' marker")
        value = m.group(1).strip()
    else:
        value = matches[-1].group(1).strip()

    if value.lower().startswith("cannot be decontextualized"):
        return DisambigResult(
            can_be_disambiguated=False,
            decontextualized_sentence=None,
            reasoning=raw.strip(),
        )
    return DisambigResult(
        can_be_disambiguated=True,
        decontextualized_sentence=value,
        reasoning=raw.strip(),
    )


_DECOMP_MARKER_PLAIN = "Specific, Verifiable, and Decontextualized Propositions:"
_DECOMP_MARKER_CTX = "Specific, Verifiable, and Decontextualized Propositions with Essential Context/Clarifications:"
_QUOTED_STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)
_CLOSING_BRACKET_RE = re.compile(r"^\]", re.MULTILINE)
_TRUE_OR_FALSE_RE = re.compile(r"\s*-\s*true or false\?\s*$", re.IGNORECASE)
_INLINE_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")


def _extract_list_after(raw: str, marker_end: int) -> list[str]:
    open_idx = raw.find("[", marker_end)
    if open_idx < 0:
        raise AdapterParseError("Decomposition output missing '[' after marker")
    close_m = _CLOSING_BRACKET_RE.search(raw, open_idx + 1)
    end_idx = close_m.start() if close_m else len(raw)
    body = raw[open_idx + 1 : end_idx]
    return [m.group(1) for m in _QUOTED_STR_RE.finditer(body)]


def parse_decomposition(raw: str) -> DecompositionResult:
    """Parse Decomposition output into `DecompositionResult`.

    The prompt emits two lists: plain propositions first, then the same
    propositions with inline [clarifications]. We prefer the clarified list
    (it is the fact-checker-ready form); inline bracketed phrases are
    concatenated into `essential_context` for callers that want them separated.
    """
    ctx_idx = raw.rfind(_DECOMP_MARKER_CTX)
    if ctx_idx < 0:
        plain_idx = raw.rfind(_DECOMP_MARKER_PLAIN)
        if plain_idx < 0:
            raise AdapterParseError(f"Decomposition output missing marker: {_DECOMP_MARKER_CTX!r}")
        items = _extract_list_after(raw, plain_idx + len(_DECOMP_MARKER_PLAIN))
        return DecompositionResult(
            claims=[
                AtomicClaim(
                    proposition=_TRUE_OR_FALSE_RE.sub("", item).strip(),
                    essential_context=None,
                )
                for item in items
            ]
        )

    items = _extract_list_after(raw, ctx_idx + len(_DECOMP_MARKER_CTX))
    claims: list[AtomicClaim] = []
    for item in items:
        text = _TRUE_OR_FALSE_RE.sub("", item).strip()
        brackets = _INLINE_BRACKET_RE.findall(text)
        essential = "; ".join(b.strip() for b in brackets) if brackets else None
        claims.append(AtomicClaim(proposition=text, essential_context=essential))
    return DecompositionResult(claims=claims)


_INVALID_SENT_CANNOT_RE = re.compile(
    r"S cannot be interpreted as a complete,?\s*declarative sentence",
    re.IGNORECASE,
)
_INVALID_SENT_CAN_RE = re.compile(
    r"S can be interpreted as a complete,?\s*declarative sentence",
    re.IGNORECASE,
)


def parse_invalid_sentence(raw: str) -> InvalidSentenceVerdict:
    """Parse the invalid_sentences prompt into a per-sentence verdict.

    The prompt prints either "S can be interpreted as a complete, declarative
    sentence" or "S cannot be interpreted...". "cannot" is checked first so it
    doesn't collide with "can".
    """
    if _INVALID_SENT_CANNOT_RE.search(raw):
        return InvalidSentenceVerdict(is_invalid=True, reasoning=raw.strip())
    if _INVALID_SENT_CAN_RE.search(raw):
        return InvalidSentenceVerdict(is_invalid=False, reasoning=raw.strip())
    raise AdapterParseError("invalid_sentences output missing can/cannot-be-interpreted verdict")


_ELEMENTS_MARKER = "What are ALL elements"


def parse_element(raw: str) -> ElementResult:
    """Parse the element prompt into an `ElementResult`.

    The prompt emits a bracketed list of `"<element> -> <verifiability>"`
    strings after the marker "What are ALL elements". We preserve each quoted
    entry verbatim; downstream coverage parsing keeps the verifiability hint.
    """
    idx = raw.rfind(_ELEMENTS_MARKER)
    if idx < 0:
        raise AdapterParseError("element output missing 'What are ALL elements' marker")
    items = _extract_list_after(raw, idx + len(_ELEMENTS_MARKER))
    return ElementResult(elements=items)


_COVERAGE_VERDICT_RE = re.compile(r"\bnot fully covered by C\b|\bfully covered by C\b", re.IGNORECASE)


def parse_coverage(raw: str, *, n_elements: int) -> CoverageResult:
    """Parse the coverage prompt into per-element covered booleans.

    The prompt emits one verdict per element in order ("fully covered by C" or
    "not fully covered by C"). `n_elements` is the expected element count so
    callers can fail fast if the model skipped or duplicated an element.
    """
    verdicts = [m.group(0).lower() for m in _COVERAGE_VERDICT_RE.finditer(raw)]
    if len(verdicts) != n_elements:
        raise AdapterParseError(f"coverage output expected {n_elements} verdicts, found {len(verdicts)}")
    return CoverageResult(per_element_covered=[v == "fully covered by c" for v in verdicts])


_ENTAILMENT_RE = re.compile(
    r"S\s+(does not\s+)?entails?\s+all\s+elements\s+of\s+C",
    re.IGNORECASE,
)


def parse_entailment(raw: str) -> EntailmentResult:
    """Parse the entailment prompt into `EntailmentResult`."""
    m = _ENTAILMENT_RE.search(raw)
    if m is None:
        raise AdapterParseError("entailment output missing 'S (does not )?entail(s) all elements of C' verdict")
    return EntailmentResult(entailed=m.group(1) is None, reasoning=raw.strip())


_CMAX_RE = re.compile(r"^C_max\s*=\s*(.+?)\s*$", re.MULTILINE)


def parse_decontextualization(raw: str, *, claim: str) -> DecontextResult:
    """Parse the decontextualization prompt into `DecontextResult`.

    The prompt emits `C_max = <text>` on its own line; `C_max = C` means no
    change, which we materialize by echoing the input claim.
    """
    matches = list(_CMAX_RE.finditer(raw))
    if not matches:
        raise AdapterParseError("decontextualization output missing 'C_max =' line")
    text = matches[-1].group(1).strip()
    if text == "C":
        text = claim
    return DecontextResult(c_max_text=text, reasoning=raw.strip())


_INVALID_CLAIM_NOT_RE = re.compile(
    r"C is not a complete,?\s*declarative sentence",
    re.IGNORECASE,
)
_INVALID_CLAIM_IS_RE = re.compile(
    r"C is a complete,?\s*declarative sentence",
    re.IGNORECASE,
)


def parse_invalid_claim(raw: str) -> InvalidClaimVerdict:
    """Parse the invalid_claims prompt into a per-claim verdict."""
    if _INVALID_CLAIM_NOT_RE.search(raw):
        return InvalidClaimVerdict(is_invalid=True, reasoning=raw.strip())
    if _INVALID_CLAIM_IS_RE.search(raw):
        return InvalidClaimVerdict(is_invalid=False, reasoning=raw.strip())
    raise AdapterParseError("invalid_claims output missing is/is-not-a-complete-declarative-sentence verdict")


T = TypeVar("T", bound=BaseModel)


def with_schema_suffix(prompt: str, model: type[T]) -> str:
    """Append a JSON-schema instruction to `prompt` for the structured path.

    pydantic-ai emits `response_format={type: 'json_schema', strict: true, ...}`
    automatically when `output_type` is a BaseModel subclass on supported
    OpenRouter backends. This suffix nudges the model to also emit JSON as a
    fallback on backends where structured output is not enforced.
    """
    schema = json.dumps(model.model_json_schema(), separators=(",", ":"))
    return (
        f"{prompt}\n\n"
        "In addition to the prose format above, return your final answer as a "
        f"JSON object matching this schema: {schema}. Place the JSON at the END "
        "of your response after a line containing only `---JSON---`."
    )
