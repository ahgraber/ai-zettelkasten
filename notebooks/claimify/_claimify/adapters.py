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
    DecompositionResult,
    DisambigResult,
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
