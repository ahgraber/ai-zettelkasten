"""Claimify evaluation agents: six per-dimension factories + bundle.

Each factory wires one of `aizk.ai.claimify.prompts.evaluation.*` to an
OpenRouter chat model via pydantic-ai. The returned async runner takes the
inputs that the prompt's USER_TEMPLATE actually uses and returns a typed
pydantic result. Adapter path ("prose" | "structured") is chosen per-factory
per the M5.5 experiment.

Aggregation of per-call verdicts into section- or claim-level result models
(`InvalidSentencesResult`, `InvalidClaimsResult`) lives in M9's orchestrator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider

from _claimify.adapters import (
    parse_coverage,
    parse_decontextualization,
    parse_element,
    parse_entailment,
    parse_invalid_claim,
    parse_invalid_sentence,
    render_template,
    with_schema_suffix,
)
from _claimify.models import (
    CoverageResult,
    DecontextResult,
    ElementResult,
    EntailmentResult,
    InvalidClaimVerdict,
    InvalidSentenceVerdict,
)
from aizk.ai.claimify.prompts.evaluation import (
    coverage as coverage_prompts,
    decontextualization as decontext_prompts,
    element as element_prompts,
    entailment as entailment_prompts,
    invalid_claims as invalid_claims_prompts,
    invalid_sentences as invalid_sentences_prompts,
)

AdapterPath = Literal["prose", "structured"]

InvalidSentenceRunner = Callable[[str, str, str], Awaitable[InvalidSentenceVerdict]]
ElementRunner = Callable[[str, str, str], Awaitable[ElementResult]]
CoverageRunner = Callable[[str, str, dict[int, str], dict[int, str]], Awaitable[CoverageResult]]
EntailmentRunner = Callable[[str, str, str, str], Awaitable[EntailmentResult]]
DecontextRunner = Callable[[str, str, str, list[str], str], Awaitable[DecontextResult]]
InvalidClaimRunner = Callable[[str], Awaitable[InvalidClaimVerdict]]


def _is_anthropic(model: str) -> bool:
    return model.startswith("anthropic/") or model.startswith("claude")


def _build_agent(
    model: str,
    *,
    api_key: str | None,
    system_prompt: str,
    result_model: type[BaseModel],
    path: AdapterPath,
) -> Agent:
    chat = OpenAIChatModel(model, provider=OpenRouterProvider(api_key=api_key))
    settings = (
        OpenAIChatModelSettings(extra_body={"cache_control": {"type": "ephemeral"}}) if _is_anthropic(model) else None
    )
    if path == "prose":
        return Agent(chat, output_type=str, system_prompt=system_prompt, model_settings=settings)
    return Agent(
        chat,
        output_type=result_model,
        system_prompt=with_schema_suffix(system_prompt, result_model),
        model_settings=settings,
    )


def _format_indexed_dict(items: dict[int, str]) -> str:
    """Render a dict as the prompt's example format: `{\\n1: "...",\\n...}`."""
    body = ",\n".join(f"{k}: {v!r}" for k, v in items.items())
    return "{\n" + body + ",\n}"


def _format_list(items: list[str]) -> str:
    body = ",\n".join(repr(i) for i in items)
    return "[\n" + body + ",\n]"


# ---------- invalid_sentences ----------


def make_invalid_sentence_agent(
    model: str, *, path: AdapterPath = "prose", api_key: str | None = None
) -> InvalidSentenceRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=invalid_sentences_prompts.SYSTEM_PROMPT,
        result_model=InvalidSentenceVerdict,
        path=path,
    )

    async def run(question: str, excerpt: str, sentence: str) -> InvalidSentenceVerdict:
        user = render_template(
            invalid_sentences_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            sentence=sentence,
        )
        result = await agent.run(user)
        if path == "prose":
            return parse_invalid_sentence(result.output)
        return result.output

    return run


# ---------- element ----------


def make_element_agent(model: str, *, path: AdapterPath = "prose", api_key: str | None = None) -> ElementRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=element_prompts.SYSTEM_PROMPT,
        result_model=ElementResult,
        path=path,
    )

    async def run(question: str, excerpt: str, sentence: str) -> ElementResult:
        user = render_template(
            element_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            sentence=sentence,
        )
        result = await agent.run(user)
        if path == "prose":
            return parse_element(result.output)
        return result.output

    return run


# ---------- coverage ----------


def make_coverage_agent(model: str, *, path: AdapterPath = "prose", api_key: str | None = None) -> CoverageRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=coverage_prompts.SYSTEM_PROMPT,
        result_model=CoverageResult,
        path=path,
    )

    async def run(
        question: str,
        excerpt: str,
        claims: dict[int, str],
        elements: dict[int, str],
    ) -> CoverageResult:
        user = render_template(
            coverage_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            claims=_format_indexed_dict(claims),
            elements=_format_indexed_dict(elements),
        )
        result = await agent.run(user)
        if path == "prose":
            return parse_coverage(result.output, n_elements=len(elements))
        return result.output

    return run


# ---------- entailment ----------


def make_entailment_agent(model: str, *, path: AdapterPath = "prose", api_key: str | None = None) -> EntailmentRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=entailment_prompts.SYSTEM_PROMPT,
        result_model=EntailmentResult,
        path=path,
    )

    async def run(question: str, excerpt: str, sentence: str, claim: str) -> EntailmentResult:
        user = render_template(
            entailment_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            sentence=sentence,
            claim=claim,
        )
        result = await agent.run(user)
        if path == "prose":
            return parse_entailment(result.output)
        return result.output

    return run


# ---------- decontextualization ----------


def make_decontextualization_agent(
    model: str, *, path: AdapterPath = "prose", api_key: str | None = None
) -> DecontextRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=decontext_prompts.SYSTEM_PROMPT,
        result_model=DecontextResult,
        path=path,
    )

    async def run(
        question: str,
        excerpt: str,
        sentence: str,
        all_claims: list[str],
        claim: str,
    ) -> DecontextResult:
        user = render_template(
            decontext_prompts.USER_TEMPLATE,
            question=question,
            excerpt=excerpt,
            sentence=sentence,
            claims=_format_list(all_claims),
            claim=claim,
        )
        result = await agent.run(user)
        if path == "prose":
            return parse_decontextualization(result.output, claim=claim)
        return result.output

    return run


# ---------- invalid_claims ----------


def make_invalid_claim_agent(
    model: str, *, path: AdapterPath = "prose", api_key: str | None = None
) -> InvalidClaimRunner:
    agent = _build_agent(
        model,
        api_key=api_key,
        system_prompt=invalid_claims_prompts.SYSTEM_PROMPT,
        result_model=InvalidClaimVerdict,
        path=path,
    )

    async def run(claim: str) -> InvalidClaimVerdict:
        user = render_template(invalid_claims_prompts.USER_TEMPLATE, claim=claim)
        result = await agent.run(user)
        if path == "prose":
            return parse_invalid_claim(result.output)
        return result.output

    return run


# ---------- bundle ----------


@dataclass(frozen=True)
class EvalAgentBundle:
    """A set of evaluation runners for one model, used by the M9 orchestrator."""

    model: str
    invalid_sentence: InvalidSentenceRunner
    element: ElementRunner
    coverage: CoverageRunner
    entailment: EntailmentRunner
    decontextualization: DecontextRunner
    invalid_claim: InvalidClaimRunner


def bundle_for(
    model: str,
    *,
    paths: dict[str, AdapterPath] | None = None,
    api_key: str | None = None,
) -> EvalAgentBundle:
    """Build an `EvalAgentBundle` for `model` with optional per-dimension paths.

    `paths` keys: "invalid_sentence", "element", "coverage", "entailment",
    "decontextualization", "invalid_claim". Missing keys default to "prose".
    """
    paths = paths or {}

    def p(key: str) -> AdapterPath:
        return paths.get(key, "prose")

    return EvalAgentBundle(
        model=model,
        invalid_sentence=make_invalid_sentence_agent(model, path=p("invalid_sentence"), api_key=api_key),
        element=make_element_agent(model, path=p("element"), api_key=api_key),
        coverage=make_coverage_agent(model, path=p("coverage"), api_key=api_key),
        entailment=make_entailment_agent(model, path=p("entailment"), api_key=api_key),
        decontextualization=make_decontextualization_agent(model, path=p("decontextualization"), api_key=api_key),
        invalid_claim=make_invalid_claim_agent(model, path=p("invalid_claim"), api_key=api_key),
    )
