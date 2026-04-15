"""Claimify extraction orchestrator: Selection -> Disambiguation -> Decomposition."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Literal

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider

from _claimify.adapters import (
    parse_decomposition,
    parse_disambiguation,
    parse_selection,
    render_template,
    with_schema_suffix,
)
from _claimify.contextualize import contextualize_section
from _claimify.io import ensure_punkt_tab
from _claimify.models import (
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
from _claimify.structuring import split_by_headings
from aizk.ai.claimify.prompts.extraction import (
    decomposition as decomposition_prompts,
    disambiguation as disambiguation_prompts,
    selection as selection_prompts,
)

logger = logging.getLogger(__name__)

AdapterPath = Literal["prose", "structured"]

SelectionRunner = Callable[[SentenceContext, str], Awaitable[SelectionResult]]
DisambigRunner = Callable[[SentenceContext, str], Awaitable[DisambigResult]]
DecompositionRunner = Callable[[SentenceContext, str], Awaitable[DecompositionResult]]


# ---------- sentence windows ----------


def build_sentence_contexts(
    section: Section,
    section_idx: int,
    *,
    p: int,
    f: int,
) -> list[SentenceContext]:
    """Build per-sentence context windows for a section using NLTK tokenization.

    `excerpt` carries the full section content; `preceding`/`following` hold
    the widest configured neighbor windows for later per-stage slicing.
    """
    ensure_punkt_tab()
    from nltk.tokenize import sent_tokenize

    sentences = sent_tokenize(section.content)
    contexts: list[SentenceContext] = []
    for i, sentence in enumerate(sentences):
        preceding = " ".join(sentences[max(0, i - p) : i])
        following = " ".join(sentences[i + 1 : i + 1 + f])
        contexts.append(
            SentenceContext(
                sentence=sentence,
                preceding=preceding,
                following=following,
                excerpt=section.content,
                section_idx=section_idx,
                sentence_idx=i,
            )
        )
    return contexts


def _windowed_excerpt(ctx: SentenceContext, *, include_following: bool) -> str:
    parts = [ctx.preceding, ctx.sentence]
    if include_following:
        parts.append(ctx.following)
    return " ".join(p for p in parts if p).strip()


# ---------- agent wiring ----------


def _is_anthropic(model: str) -> bool:
    return model.startswith("anthropic/") or model.startswith("claude")


def _chat_model(model: str, *, api_key: str | None) -> OpenAIChatModel:
    return OpenAIChatModel(model, provider=OpenRouterProvider(api_key=api_key))


def _settings_for(model: str) -> OpenAIChatModelSettings | None:
    if _is_anthropic(model):
        return OpenAIChatModelSettings(extra_body={"cache_control": {"type": "ephemeral"}})
    return None


def _make_stage_runner(
    model: str,
    *,
    path: AdapterPath,
    api_key: str | None,
    system_prompt: str,
    user_template: str,
    result_model: type,
    parse_raw: Callable[[str, str], object],
    include_following: bool,
):
    settings = _settings_for(model)
    chat = _chat_model(model, api_key=api_key)
    if path == "prose":
        agent = Agent(chat, output_type=str, system_prompt=system_prompt, model_settings=settings)
    else:
        agent = Agent(
            chat,
            output_type=result_model,
            system_prompt=with_schema_suffix(system_prompt, result_model),
            model_settings=settings,
        )

    async def run(ctx: SentenceContext, question: str):
        excerpt = _windowed_excerpt(ctx, include_following=include_following)
        user = render_template(user_template, question=question, excerpt=excerpt, sentence=ctx.sentence)
        result = await agent.run(user)
        if path == "prose":
            return parse_raw(result.output, ctx.sentence)
        return result.output

    return run


def make_selection_runner(model: str, *, path: AdapterPath = "prose", api_key: str | None = None) -> SelectionRunner:
    return _make_stage_runner(
        model,
        path=path,
        api_key=api_key,
        system_prompt=selection_prompts.SYSTEM_PROMPT,
        user_template=selection_prompts.USER_TEMPLATE,
        result_model=SelectionResult,
        parse_raw=lambda raw, sentence: parse_selection(raw, sentence=sentence),
        include_following=True,
    )


def make_disambiguation_runner(
    model: str, *, path: AdapterPath = "prose", api_key: str | None = None
) -> DisambigRunner:
    return _make_stage_runner(
        model,
        path=path,
        api_key=api_key,
        system_prompt=disambiguation_prompts.SYSTEM_PROMPT,
        user_template=disambiguation_prompts.USER_TEMPLATE,
        result_model=DisambigResult,
        parse_raw=lambda raw, _sentence: parse_disambiguation(raw),
        include_following=False,
    )


def make_decomposition_runner(
    model: str, *, path: AdapterPath = "prose", api_key: str | None = None
) -> DecompositionRunner:
    return _make_stage_runner(
        model,
        path=path,
        api_key=api_key,
        system_prompt=decomposition_prompts.SYSTEM_PROMPT,
        user_template=decomposition_prompts.USER_TEMPLATE,
        result_model=DecompositionResult,
        parse_raw=lambda raw, _sentence: parse_decomposition(raw),
        include_following=False,
    )


# ---------- orchestrator ----------


def default_question(doc: LoadedDoc, section: Section) -> str:
    if section.heading_path:
        path = " > ".join(section.heading_path)
        return f"What does the section '{path}' in '{doc.title}' describe?"
    return f"What does '{doc.title}' describe?"


async def _process_sentence(
    doc: LoadedDoc,
    ctx: SentenceContext,
    question: str,
    *,
    selection: SelectionRunner,
    disambiguation: DisambigRunner,
    decomposition: DecompositionRunner,
    heading_path: list[str],
    context_str: str,
    sem: asyncio.Semaphore,
) -> list[ExtractionRecord]:
    async with sem:
        try:
            sel = await selection(ctx, question)
        except Exception as exc:
            return [_fail_record(doc, ctx, "selection", exc)]
        if not sel.contains_proposition:
            return []

        try:
            dis = await disambiguation(ctx, question)
        except Exception as exc:
            return [_fail_record(doc, ctx, "disambiguation", exc)]
        if not dis.can_be_disambiguated or dis.decontextualized_sentence is None:
            return []

        try:
            dec = await decomposition(ctx, question)
        except Exception as exc:
            return [_fail_record(doc, ctx, "decomposition", exc)]

        return [
            ClaimRecord(
                claim=ExtractedClaim(
                    doc_uuid=doc.aizk_uuid,
                    heading_path=heading_path,
                    section_idx=ctx.section_idx,
                    sentence_idx=ctx.sentence_idx,
                    sentence=ctx.sentence,
                    claim=claim,
                    context_str=context_str,
                )
            )
            for claim in dec.claims
        ]


def _fail_record(
    doc: LoadedDoc,
    ctx: SentenceContext,
    stage: Literal["selection", "disambiguation", "decomposition", "contextualize"],
    exc: BaseException,
) -> FailedRecord:
    logger.warning(
        "Claim extraction failed at stage=%s doc=%s section=%d sentence=%d: %s",
        stage,
        doc.aizk_uuid,
        ctx.section_idx,
        ctx.sentence_idx,
        exc,
    )
    return FailedRecord(
        failure=FailedExtraction(
            doc_uuid=doc.aizk_uuid,
            section_idx=ctx.section_idx,
            sentence_idx=ctx.sentence_idx,
            stage=stage,
            error=f"{type(exc).__name__}: {exc}",
        )
    )


async def extract_claims(
    doc: LoadedDoc,
    *,
    context_agent: Agent,
    selection: SelectionRunner,
    disambiguation: DisambigRunner,
    decomposition: DecompositionRunner,
    p: int = 5,
    f: int = 5,
    question_for: Callable[[LoadedDoc, Section], str] = default_question,
    max_parallel_sentences: int = 8,
) -> list[ExtractionRecord]:
    """Run the full Claimify extraction pipeline for one document.

    Sections run serially to keep the contextualizer's system prompt cache
    warm; sentences within a section fan out under a per-section semaphore.

    TODO(claimify-tabular): extract claims from table content + caption
    TODO(claimify-code):    extract claims from code + language + surrounding prose
    TODO(claimify-image):   extract claims from alt-text + caption + optional VLM description
    """
    sections = split_by_headings(doc.markdown)
    records: list[ExtractionRecord] = []
    sem = asyncio.Semaphore(max_parallel_sentences)

    for section_idx, section in enumerate(sections):
        question = question_for(doc, section)
        try:
            context_str = await contextualize_section(context_agent, doc, section)
        except Exception as exc:
            logger.warning(
                "Contextualization failed for doc=%s section=%d: %s",
                doc.aizk_uuid,
                section_idx,
                exc,
            )
            records.append(
                FailedRecord(
                    failure=FailedExtraction(
                        doc_uuid=doc.aizk_uuid,
                        section_idx=section_idx,
                        sentence_idx=-1,
                        stage="contextualize",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            )
            continue

        contexts = build_sentence_contexts(section, section_idx, p=p, f=f)
        heading_path = list(section.heading_path)
        batched = await asyncio.gather(
            *[
                _process_sentence(
                    doc,
                    ctx,
                    question,
                    selection=selection,
                    disambiguation=disambiguation,
                    decomposition=decomposition,
                    heading_path=heading_path,
                    context_str=context_str,
                    sem=sem,
                )
                for ctx in contexts
            ]
        )
        for item in batched:
            records.extend(item)

    return records
